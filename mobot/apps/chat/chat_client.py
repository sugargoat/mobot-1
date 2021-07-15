import re
import logging
from enum import Enum
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Any, Iterable
from copy import deepcopy
from typing import Set

import mobilecoin

from django.conf import settings
from django.utils import timezone

from .models import MobotBot, MobotChatSession
from mobot.apps.merchant_services.models import Customer, CustomerStorePreferences, DropSession, Campaign, Store
from mobot.signald_client import Signal
from mobot.lib.signal import SignalCustomerDataClient
from mobot.signald_client.types import Message as SignalMessage
from mobot.signald_client.main import QueueSubscriber
from mobot.apps.chat.context import MessageContextManager, MobotContext
from .handlers import *
from .utils import *


class TransactionStatus(str, Enum):
    TRANSACTION_SUCCESS = "TransactionSuccess"
    TRANSACTION_PENDING = "TransactionPending"


class MobotMessage(str, Enum):
    YES = "y"
    NO = "n"
    CANCEL = "cancel"
    UNSUBSCRIBE = "unsubscribe"


class NoMatchingHandlerException(Exception):
    pass


class MobotDispatcher:
    def __init__(self,
                 name: str,
                 method: Callable[[MobotContext], Any],
                 order: int = 100,
                 conditions: Iterable[Callable[[MobotContext], bool]] = [],
                 text_filtered: bool = False,
                 ):
        self.name = name
        self._method = method
        self.ctx_conditions = conditions
        self.order = order
        self.text_filtered = text_filtered

    @property
    def catch_all(self) -> bool:
        return len(self.ctx_conditions) == 0

    def context_matches(self, context: MobotContext):
        match = all([cond(context) for cond in self.ctx_conditions]) if self.ctx_conditions else True
        return match

    def handle(self, context: MobotContext):
        return self._method(context)

    def __str__(self):
        return f"{self.name} Handler"


class Mobot:
    def __init__(self, signal: Signal, mobilecoin_client: mobilecoin.Client, store: Store, campaign: Campaign, subscriber: QueueSubscriber = None):
        self.name = f"Mobot-{store.name}"
        self.logger = logging.getLogger(f"Mobot-{store.id}")
        self.signal = signal
        self.campaign = campaign
        self.store = store
        self.mobilecoin_client = mobilecoin_client
        self.mobot: MobotBot = self.get_mobot_bot()
        self.message_context_manager = MessageContextManager(self.mobot, self.logger, self.signal)
        self.customer_data_client = SignalCustomerDataClient(signal=self.signal)
        self._subscriber = subscriber if subscriber else QueueSubscriber(self.name)
        self._executor_futures = []
        self.handlers = []

    def get_context_from_message(self, message: SignalMessage) -> MobotContext:
        context: MobotContext = self.message_context_manager.get_message_context(message)
        return context

    def get_context_from_customer(self, customer: Customer) -> MobotContext:
        context = self.message_context_manager.get_message_context(message=None, customer=customer)
        return context

    def get_mobot_bot(self) -> MobotBot:
        bot, _ = MobotBot.objects.get_or_create(name=f"{self.store.name}-Bot", store=self.store, campaign=self.campaign)
        return bot

    def register_handler(self, name: str,  method: Callable[[MobotContext], Any], regex: str="", order: int = 100,
                         chat_session_states: Set[MobotChatSession.State] = set(),
                         drop_session_states: Set[DropSession.State] = set(),
                         ctx_conditions: List[MobotContextFilter] = []):
        conditions = ctx_conditions.copy()
        if regex:
            conditions.add(regex_filter(regex))
        if drop_session_states:
            conditions.add(drop_session_state_filter(drop_session_states))
        if chat_session_states:
            conditions.add(chat_session_state_filter(chat_session_states))

        dispatch_handler = MobotDispatcher(
                                  name,
                                  method,
                                  text_filtered=True if regex else False,
                                  order=order,
                                  conditions=tuple(conditions))

        self.handlers.append(dispatch_handler)


    def set_customer_preferences(self, customer: Customer, allow_contact: bool) -> CustomerStorePreferences:
        customer_prefs = CustomerStorePreferences.objects.get_or_create(customer=customer, store=self.store)
        customer_prefs.allows_contact = allow_contact
        customer_prefs.save()
        return customer_prefs

    def find_active_campaigns(self):
        Campaign.objects.filter(start_time__gte=timezone.now(), end_time__lte=timezone.now())

    def _handle_chat(self, message: SignalMessage):
        # TODO: Would be great to cache these after they're hit... One day.
        self.logger.debug(f"Attempting to match message: {message.text}")
        context = self.get_context_from_message(message)
        with context:
            matching_handlers = []
            for handler in self.handlers:
                if handler.context_matches(context) and handler.text_filtered:
                    # First, check for explicit text
                    matching_handlers.append(handler)
            if not matching_handlers:
                for handler in self.handlers:
                    if handler.context_matches(context):
                        matching_handlers.append(handler)
            matching_handlers.sort(key=lambda matched_handler: matched_handler.order)
            for handler in matching_handlers:
                try:
                    handler.handle(context)
                except Exception:
                    self.logger.exception(f"Failed to run handler for {handler.name}")
            if not matching_handlers:
                raise NoMatchingHandlerException(message.text)

    def register_default_handlers(self):
        self.register_handler(name="unsubscribe", regex="^(u|unsubscribe)$", method=unsubscribe_handler)
        self.register_handler(name="subscribe", regex="^(s|subscribe)$", method=subscribe_handler)
        self.register_handler(name="greet", method=handle_greet_customer, chat_session_states={MobotChatSession.State.NOT_GREETED}, order=1)  # First, say hello to the customer
        self.register_handler(name="start", method=handle_start_conversation, chat_session_states={MobotChatSession.State.NOT_GREETED},
                              order=2)  # Then, handle setting up drop session
        self.register_handler(name="already", method=handle_already_greeted,
                              chat_session_states={MobotChatSession.State.INTRODUCTION_GIVEN})
        self.register_handler(name="expired",  method=handle_drop_expired, drop_session_states={DropSession.State.EXPIRED})
        self.register_handler(name="not_ready", method=handle_drop_not_ready, drop_session_states={DropSession.State.NOT_READY})
        self.register_handler(name="no_other_handler", method=handle_no_handler_found, chat_session_states={MobotChatSession.State.INTRODUCTION_GIVEN})
        self.register_handler(name="privacy",  regex="^p$", method=privacy_policy_handler)
        self.register_handler(name="inventory", regex="^(i|inventory)$", method=inventory_handler,
                              drop_session_states={DropSession.State.ACCEPTED, DropSession.State.OFFERED})

    def find_and_greet_targets(self, campaign):
        for customer in self.campaign.get_target_customers():
            preferences, created = CustomerStorePreferences.objects.get_or_create(customer=customer,
                                                                                  store_ref=campaign.store)

            self.logger.info("Reaching out to existing customers if they pass target validation")
            ctx = self.get_context_from_customer(customer)
            if ctx.drop_session.state == DropSession.State.CREATED:
                ctx.log_and_send_message(ChatStrings.GREETING.format(store=self.store,
                                                                     campaign=campaign,
                                                                     campaign_description=campaign.description))



    def run(self, max_messages=0):
        self.signal.register_subscriber(self._subscriber)
        self.register_default_handlers()
        with ThreadPoolExecutor(4) as executor:
            self._executor_futures.append(executor.submit(self.signal.run_chat, True))
            while True:
                for message in self._subscriber.receive_messages():
                    self.logger.debug(f"Mobot received message: {message}")
                    self._executor_futures.append(executor.submit(self.find_and_greet_targets, self.campaign))
                    # Handle in foreground while I'm testing
                    if settings.TEST:
                        self._handle_chat(message)
                    else:
                        self._executor_futures.append(executor.submit(self._handle_chat, message))
                    if max_messages:
                        if self._subscriber.total_received == max_messages:
                            executor.shutdown(wait=True)
                            return
            executor.shutdown(wait=True)
