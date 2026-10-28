"""Offline checks for operational logs; all data here is synthetic."""
import logging

from app import fixtures
from app.bot import GroupAgent
from app.main import log_poll_ingress
from app.store import Store
from app.telegram import TelegramClient, SendOutcome, SendStatus, PythonTelegramBotTransport
from telegram.error import BadRequest, NetworkError, TimedOut
import pytest
from conftest import run


def test_ingress_logs_counts_and_rejected_chat_types(config, transport, caplog):
    store = Store(":memory:")
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt="", agent_factory=lambda spec: None)
    message = fixtures.message(update_id=501, message_id=501, sender_id=1, username="synthetic",
                               text="private message sentinel")
    rejected = fixtures.message(update_id=502, message_id=502, chat_id=987654321,
                                sender_id=1, username="synthetic", text="rejected message sentinel")
    reaction = {"update_id": 503, "message_reaction": {
        "chat": {"id": 987654322, "type": "private"}, "message_id": 1,
        "old_reaction": [], "new_reaction": []}}
    with caplog.at_level(logging.INFO):
        tick, _ = run(bot.step([message, message, rejected, reaction], run=False))
        log_poll_ingress(tick, paused=False)
    assert (tick.updates, tick.messages_stored, tick.duplicates, tick.unauthorized) == (4, 1, 1, 2)
    assert "chat_id=987654321 chat_type=supergroup" in caplog.text
    assert "chat_id=987654322 chat_type=private update_type=message_reaction" in caplog.text
    assert "updates=4 stored=1 duplicates=1 unauthorized=2" in caplog.text
    assert "sends run in background" in caplog.text
    assert "0 sends" not in caplog.text
    assert "message sentinel" not in caplog.text
    store.close()


def test_autonomous_send_logs_actual_status_without_content(config, transport, caplog):
    store = Store(":memory:")
    client = TelegramClient(transport)
    bot = GroupAgent(config=config, store=store, client=client,
                     system_prompt="", agent_factory=lambda spec: None)

    async def send_message(**kwargs):
        return SendOutcome(SendStatus.UNKNOWN, error="remote response secret")

    client.send_message = send_message
    with caplog.at_level(logging.INFO):
        outcome = run(bot._send_action(chat_id=123, topic_id=5, text="private output sentinel", reply_to=None))
    assert outcome.status == SendStatus.UNKNOWN
    assert "autonomous send chat_id=123 topic_id=5 status=unknown dry_run=False" in caplog.text
    assert "private output sentinel" not in caplog.text
    assert "remote response secret" not in caplog.text
    store.close()


@pytest.mark.parametrize("error, status, category", [
    (TimedOut("private detail token-sentinel"), SendStatus.UNKNOWN, "timeout"),
    (NetworkError("private detail token-sentinel"), SendStatus.UNKNOWN, "network"),
    (BadRequest("private detail token-sentinel"), SendStatus.FAILED, "api_rejected"),
])
def test_transport_category_reaches_action_log(config, caplog, error, status, category):
    class FailingBot:
        async def send_message(self, **kwargs):
            raise error

    store = Store(":memory:")
    client = TelegramClient(PythonTelegramBotTransport(FailingBot()))
    bot = GroupAgent(config=config, store=store, client=client,
                     system_prompt="", agent_factory=lambda spec: None)
    with caplog.at_level(logging.INFO):
        outcome = run(bot._send_action(chat_id=123, topic_id=None,
                                       text="private body sentinel", reply_to=None))
    assert outcome.status is status
    assert outcome.error_category == category
    assert f"error_category={category}" in caplog.text
    assert "private detail" not in caplog.text
    assert "token-sentinel" not in caplog.text
    assert "private body sentinel" not in caplog.text
    store.close()


def test_untrusted_category_is_not_logged(config, transport, caplog):
    store = Store(":memory:")
    client = TelegramClient(transport)
    bot = GroupAgent(config=config, store=store, client=client,
                     system_prompt="", agent_factory=lambda spec: None)

    async def send_message(**kwargs):
        return SendOutcome(SendStatus.FAILED, None, "secret detail", "secret category")

    client.send_message = send_message
    with caplog.at_level(logging.INFO):
        run(bot._send_action(chat_id=123, topic_id=None, text="secret body", reply_to=None))
    assert "error_category=unknown" in caplog.text
    assert "secret" not in caplog.text
    store.close()


def test_autonomous_exception_logs_type_and_stage_without_detail(config, transport, caplog):
    store = Store(":memory:")
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt="", agent_factory=lambda spec: None)

    class FailingAutonomy:
        async def tick(self, **_kwargs):
            raise ValueError("secret token and message body")

    bot.autonomy = FailingAutonomy()
    with caplog.at_level(logging.ERROR):
        run(bot._autonomous_pass())
    assert "stage=autonomous_pass error_type=ValueError" in caplog.text
    assert "secret token and message body" not in caplog.text
    store.close()
