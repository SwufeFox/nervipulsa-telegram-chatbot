"""The PTB transport, exercised against a stub Bot.

No network: the point is the mapping — which PTB method each of our four calls
becomes, and how PTB's exception tree lands on sent/failed/unknown.
"""
from __future__ import annotations

import asyncio

import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

from app.telegram import (
    PythonTelegramBotTransport,
    SendStatus,
    TelegramClient,
    TransportError,
)


def run(coro):
    return asyncio.run(coro)


class StubBot:
    """Just enough of `telegram.Bot` for the transport's four methods."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail: Exception | None = None
        self.fail_initialize: Exception | None = None
        self.initialized = 0
        self.shutdowns = 0
        self.updates: list = []

    async def initialize(self) -> None:
        if self.fail_initialize is not None:
            raise self.fail_initialize
        self.initialized += 1

    async def shutdown(self) -> None:
        self.shutdowns += 1

    def _maybe_fail(self, name: str, payload: dict) -> None:
        self.calls.append((name, payload))
        if self.fail is not None:
            raise self.fail

    async def get_updates(self, **kwargs):
        self._maybe_fail("get_updates", kwargs)
        return self.updates

    async def send_message(self, **kwargs):
        self._maybe_fail("send_message", kwargs)

        class _Message:
            message_id = 4242

        return _Message()

    async def get_me(self):
        self._maybe_fail("get_me", {})

        class _User:
            def to_dict(self) -> dict:
                return {"id": 99, "is_bot": True, "username": "alma_bot"}

        return _User()

    async def delete_message(self, **kwargs):
        self._maybe_fail("delete_message", kwargs)
        return True


def test_lifecycle_is_explicit_not_implicit() -> None:
    bot = StubBot()
    client = TelegramClient(PythonTelegramBotTransport(bot))

    async def body() -> None:
        await client.start()
        await client.close()

    run(body())
    assert (bot.initialized, bot.shutdowns) == (1, 1)


def test_get_updates_forwards_subscription_and_gives_ptb_a_longer_read_timeout() -> None:
    bot = StubBot()
    client = TelegramClient(PythonTelegramBotTransport(bot))

    async def body():
        return await client.get_updates(offset=17, timeout=25)

    updates = run(body())
    assert updates == []
    name, kwargs = bot.calls[0]
    assert name == "get_updates"
    assert kwargs["offset"] == 17
    assert kwargs["allowed_updates"] == [
        "message", "edited_message", "message_reaction",
    ]
    # PTB rejects a read timeout that does not exceed the poll window.
    assert kwargs["read_timeout"] > kwargs["timeout"]


def test_send_maps_topic_and_reply_into_ptb_arguments() -> None:
    bot = StubBot()
    client = TelegramClient(PythonTelegramBotTransport(bot))

    async def body():
        return await client.send_message(
            chat_id=-100, text="hi", reply_to_message_id=7, topic_id=12
        )

    outcome = run(body())
    assert outcome.status is SendStatus.SENT
    assert outcome.telegram_message_id == 4242
    _, kwargs = bot.calls[0]
    assert kwargs["chat_id"] == -100
    assert kwargs["message_thread_id"] == 12
    assert kwargs["reply_parameters"].message_id == 7
    assert kwargs["reply_parameters"].allow_sending_without_reply is True


@pytest.mark.parametrize(
    "error, expected",
    [
        (TimedOut("read timed out"), SendStatus.UNKNOWN),
        (NetworkError("connection reset"), SendStatus.UNKNOWN),
        (BadRequest("chat not found"), SendStatus.FAILED),
    ],
)
def test_send_classifies_ptb_errors(error: Exception, expected: SendStatus) -> None:
    """A timeout may have delivered; a BadRequest certainly did not."""
    bot = StubBot()
    bot.fail = error
    client = TelegramClient(PythonTelegramBotTransport(bot))

    async def body():
        return await client.send_message(chat_id=-100, text="hi")

    outcome = run(body())
    assert outcome.status is expected


def test_dry_run_never_touches_ptb() -> None:
    bot = StubBot()
    client = TelegramClient(PythonTelegramBotTransport(bot), dry_run=True)

    async def body():
        return await client.send_message(chat_id=-100, text="hi")

    outcome = run(body())
    assert outcome.status is SendStatus.SENT
    assert bot.calls == []


def test_unsupported_method_is_a_loud_rejection() -> None:
    transport = PythonTelegramBotTransport(StubBot())

    with pytest.raises(TransportError) as caught:
        run(transport.call("setWebhook", {"url": "https://example.com"}))
    assert caught.value.kind == "rejected"


def test_network_fault_never_leaks_the_token() -> None:
    """PTB puts the URL in some messages; the transport must scrub it."""
    bot = StubBot()
    bot.fail = NetworkError("https://api.telegram.org/bot123456:AAsecretToken/getMe failed")
    transport = PythonTelegramBotTransport(bot)

    with pytest.raises(TransportError) as caught:
        run(transport.call("getMe", {}))

    detail = caught.value.detail
    assert "AAsecretToken" not in detail
    assert "123456:" not in detail
    assert "<redacted>" in detail


def test_startup_rejection_is_a_transport_error_without_the_token() -> None:
    """`Bot.initialize()` calls `getMe`, and `InvalidToken` quotes the token.

    `run_live` prints this detail to the console, so it must not be the raw
    PTB message — and a bad token has to arrive as our error, not as a crash.
    """
    bot = StubBot()
    bot.fail_initialize = BadRequest(
        "The token `123456:AAsecretTokenValueHere12345` was rejected by the server."
    )
    client = TelegramClient(PythonTelegramBotTransport(bot))

    with pytest.raises(TransportError) as caught:
        run(client.start())
    assert caught.value.kind == "rejected"
    assert "AAsecretTokenValueHere12345" not in caught.value.detail


def test_startup_network_fault_is_indeterminate_not_a_rejection() -> None:
    bot = StubBot()
    bot.fail_initialize = NetworkError("connection reset")
    client = TelegramClient(PythonTelegramBotTransport(bot))

    with pytest.raises(TransportError) as caught:
        run(client.start())
    assert caught.value.kind == "indeterminate"
