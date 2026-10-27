"""Telegram long polling and send, on top of python-telegram-bot.

Two things this module is careful about:

**Update parsing** is total. Anything it does not understand becomes `None`
rather than an exception, because a bot that dies on a service message is a bot
that loses its poll cursor.

**Send outcomes are three-valued.** A send either succeeded (we hold a message
id), was definitively rejected by Telegram (4xx / a PTB `TelegramError` that is
not a transport fault), or is *unknown* (timeout, connection reset, 5xx, a
process that died mid-request). Collapsing unknown into failure is how a bot
posts the same message twice.

**Why the transport is still injectable.** `python-telegram-bot` owns the HTTP
session in production, but the application's durability tests need to script
failures. So `TelegramClient` accepts either an async transport (PTB) or the
older synchronous `call(method, payload)` fake, and runs the latter in a worker
thread. `getUpdates` is deliberately still driven by our own loop: the durable
inbox and the persisted offset have to be committed before the cursor moves,
which is exactly what an `Updater`/`Application` polling queue would do for us
too early.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Mapping, Protocol

from telegram import Bot, ReplyParameters
from telegram.error import (
    BadRequest,
    NetworkError as TelegramNetworkError,
    TelegramError,
    TimedOut as TelegramTimedOut,
)

# Bot tokens appear inside Bot API URLs as `/bot<id>:<secret>/`, and PTB also
# quotes the raw token in a few messages ("The token `123:abc` was rejected").
# Both shapes are scrubbed here, matched by shape rather than by known value so
# this works for any token. This runs before any string reaches a log or the
# `sends` table, because a rejected token is exactly the case where a startup
# error gets pasted into an issue.
_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
_BARE_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")


class SendStatus(StrEnum):
    SENT = "sent"
    FAILED = "failed"     # Telegram said no; a retry is a new decision
    UNKNOWN = "unknown"   # we do not know; never auto-retry


@dataclass(frozen=True)
class SendOutcome:
    status: SendStatus
    telegram_message_id: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class InboundMessage:
    """One chat message extracted from an update. Platform fields only."""

    update_id: int
    chat_id: int
    message_id: int
    topic_id: int | None
    sender_id: int | None
    sender_label: str
    is_bot: bool
    text: str
    reply_to_message_id: int | None
    reply_to_sender_id: int | None
    created_at: float
    chat_type: str
    chat_title: str
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class InboundReaction:
    """A reaction change without actor identity or raw Telegram payload."""

    update_id: int
    chat_id: int
    message_id: int
    old_vote: str | None
    new_vote: str | None


class Transport(Protocol):
    """Either PTB's async transport, or a synchronous `call` fake."""

    def call(
        self, method: str, payload: dict[str, Any]
    ) -> dict[str, Any] | Awaitable[dict[str, Any]]: ...


class TransportError(Exception):
    def __init__(self, *, kind: str, detail: str, status: int | None = None) -> None:
        super().__init__(detail)
        self.kind = kind            # "rejected" | "indeterminate"
        self.detail = detail
        self.status = status


def _short(exc: BaseException) -> str:
    """A message that is safe to log: never carries the token.

    Some transport errors quote the request URL, and the URL contains the bot
    token. That string ends up in logs and in the send record, so it is
    scrubbed here rather than trusted to be absent.
    """
    text = str(exc).strip() or exc.__class__.__name__
    text = _TOKEN_IN_URL.sub("/bot<redacted>", text)
    text = _BARE_TOKEN.sub("<redacted-token>", text)
    return text[:300]


class PythonTelegramBotTransport:
    """The production transport: python-telegram-bot's async `Bot`.

    PTB owns the HTTP session, timeouts and response typing. This class only
    maps the four calls the application makes onto it, and maps PTB's exception
    tree onto the `rejected` / `indeterminate` split the rest of the app relies
    on. `initialize()`/`shutdown()` are exposed as `start()`/`close()` so the
    session is opened and closed by the caller's lifecycle, not by import.
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def start(self) -> None:
        """Open the session.

        PTB's `initialize()` calls `getMe` by itself, so a bad token fails here
        rather than at the first poll. It is wrapped for the same reason as
        every other call: `InvalidToken` quotes the raw token in its message,
        and this string is printed to the console by `run_live`.
        """
        try:
            await self._bot.initialize()
        except TelegramError as exc:
            unreachable = isinstance(
                exc, (TelegramTimedOut, TelegramNetworkError)
            ) and not isinstance(exc, BadRequest)
            raise TransportError(
                kind="indeterminate" if unreachable else "rejected",
                detail=_short(exc),
            ) from exc

    async def close(self) -> None:
        await self._bot.shutdown()

    async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._dispatch(method, payload)
        except TransportError:
            raise
        except TelegramError as exc:
            # `BadRequest` subclasses `NetworkError`, so the specific sentence
            # ("Telegram answered and said no") has to be tested first. Getting
            # this order wrong marks a real rejection as undelivered-unknown,
            # which then never gets retried and hides a fixable error.
            if isinstance(exc, BadRequest):
                raise TransportError(kind="rejected", detail=_short(exc)) from exc
            if isinstance(exc, (TelegramTimedOut, TelegramNetworkError)):
                # The request may or may not have reached Telegram.
                raise TransportError(kind="indeterminate", detail=_short(exc)) from exc
            # Any other API error is a refusal carrying a description.
            raise TransportError(kind="rejected", detail=_short(exc)) from exc

    async def _dispatch(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "getUpdates":
            timeout = payload.get("timeout")
            timeout = int(timeout) if isinstance(timeout, int) and timeout > 0 else 0
            updates = await self._bot.get_updates(
                offset=payload.get("offset"),
                limit=payload.get("limit"),
                timeout=timeout or None,
                allowed_updates=payload.get("allowed_updates"),
                # PTB insists the read timeout outlive the long-poll window.
                read_timeout=float(timeout) + 10.0 if timeout else None,
            )
            return {"ok": True, "result": [update.to_dict() for update in updates]}

        if method == "sendMessage":
            reply_parameters = None
            raw_reply = payload.get("reply_parameters")
            if isinstance(raw_reply, Mapping) and isinstance(raw_reply.get("message_id"), int):
                reply_parameters = ReplyParameters(
                    message_id=raw_reply["message_id"],
                    allow_sending_without_reply=bool(
                        raw_reply.get("allow_sending_without_reply", False)
                    ),
                )
            message = await self._bot.send_message(
                chat_id=payload["chat_id"],
                text=payload["text"],
                message_thread_id=payload.get("message_thread_id"),
                reply_parameters=reply_parameters,
            )
            return {"ok": True, "result": {"message_id": message.message_id}}

        if method == "getMe":
            user = await self._bot.get_me()
            return {"ok": True, "result": user.to_dict()}

        if method == "deleteMessage":
            deleted = await self._bot.delete_message(
                chat_id=payload["chat_id"], message_id=payload["message_id"]
            )
            return {"ok": True, "result": bool(deleted)}

        # Nothing else is used by this application. Failing loudly beats a
        # silently ignored call the caller believes succeeded.
        raise TransportError(
            kind="rejected", detail=f"unsupported Telegram method: {method}"
        )


class TelegramClient:
    """The Bot API surface this application actually uses, as coroutines.

    `transport` is either an async transport (PTB) or a synchronous
    `call(method, payload)` fake, which is run in a worker thread.
    """

    def __init__(self, transport: Transport | PythonTelegramBotTransport, *,
                 dry_run: bool = False) -> None:
        self._transport = transport
        self.dry_run = dry_run
        self._is_async = inspect.iscoroutinefunction(getattr(transport, "call", None))
        self._dry_run_counter = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Open the transport's session, if it has one."""
        hook = getattr(self._transport, "start", None)
        if hook is not None:
            result = hook()
            if inspect.isawaitable(result):
                await result

    async def close(self) -> None:
        """Close the transport's session, if it has one."""
        hook = getattr(self._transport, "close", None)
        if hook is not None:
            result = hook()
            if inspect.isawaitable(result):
                await result

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._is_async:
            return await self._transport.call(method, payload)
        return await asyncio.to_thread(self._transport.call, method, payload)

    # -- inbound -----------------------------------------------------------

    async def get_updates(
        self, *, offset: int | None, timeout: int = 25, limit: int = 50
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "limit": limit,
            "allowed_updates": ["message", "edited_message", "message_reaction"],
        }
        if offset is not None:
            payload["offset"] = offset
        response = await self._call("getUpdates", payload)
        result = response.get("result")
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    # -- outbound ----------------------------------------------------------

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        topic_id: int | None = None,
    ) -> SendOutcome:
        """Send, and classify the outcome into sent / failed / unknown."""
        if self.dry_run:
            self._dry_run_counter += 1
            return SendOutcome(SendStatus.SENT, telegram_message_id=-self._dry_run_counter)

        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        if topic_id is not None:
            payload["message_thread_id"] = topic_id
        try:
            response = await self._call("sendMessage", payload)
        except TransportError as exc:
            if exc.kind == "rejected":
                return SendOutcome(SendStatus.FAILED, error=exc.detail)
            # Timeout / reset / 5xx / unparseable: the message may be visible.
            return SendOutcome(SendStatus.UNKNOWN, error=exc.detail)
        result = response.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if isinstance(message_id, int):
            return SendOutcome(SendStatus.SENT, telegram_message_id=message_id)
        return SendOutcome(SendStatus.UNKNOWN, error="no message_id in response")

    async def get_me(self) -> dict[str, Any]:
        return (await self._call("getMe", {})).get("result") or {}

    async def delete_message(self, *, chat_id: int, message_id: int) -> bool:
        try:
            await self._call(
                "deleteMessage", {"chat_id": chat_id, "message_id": message_id}
            )
            return True
        except TransportError:
            return False


# -- parsing ---------------------------------------------------------------


def parse_update(update: Mapping[str, Any]) -> InboundMessage | None:
    """Extract a chat message from an update. Returns None for anything else.

    Covers plain messages, forum topic messages (`is_topic_message` +
    `message_thread_id`), and `reply_to_message`. Edited messages are treated as
    new input: the user changed what they said, which is worth re-reading.
    """
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return None
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, Mapping):
        return None

    chat = message.get("chat")
    if not isinstance(chat, Mapping):
        return None
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return None

    sender = message.get("from") if isinstance(message.get("from"), Mapping) else {}
    sender_id = sender.get("id") if isinstance(sender.get("id"), int) else None
    sender_label = _display_name(sender) or (str(chat.get("title") or "unknown"))

    # `message_thread_id` is the forum topic; in a private chat it can be the
    # topic of a linked discussion, which is also the right grouping key.
    topic_id = message.get("message_thread_id")
    topic_id = topic_id if isinstance(topic_id, int) and message.get("is_topic_message") else None

    body = message.get("text")
    if not isinstance(body, str):
        caption = message.get("caption")
        body = caption if isinstance(caption, str) else ""

    reply_to = message.get("reply_to_message")
    reply_to_id = None
    reply_to_sender = None
    if isinstance(reply_to, Mapping):
        rid = reply_to.get("message_id")
        reply_to_id = rid if isinstance(rid, int) else None
        origin = reply_to.get("from")
        if isinstance(origin, Mapping) and isinstance(origin.get("id"), int):
            reply_to_sender = origin["id"]

    date = message.get("date")

    return InboundMessage(
        update_id=update_id,
        chat_id=chat_id,
        message_id=message_id,
        topic_id=topic_id,
        sender_id=sender_id,
        sender_label=sender_label,
        is_bot=bool(sender.get("is_bot")),
        text=body,
        reply_to_message_id=reply_to_id,
        reply_to_sender_id=reply_to_sender,
        created_at=float(date) if isinstance(date, int) else 0.0,
        chat_type=str(chat.get("type") or ""),
        chat_title=str(chat.get("title") or ""),
        raw=update,
    )


def _display_name(user: Mapping[str, Any]) -> str:
    username = user.get("username")
    if isinstance(username, str) and username:
        return username
    parts = [
        user.get("first_name"),
        user.get("last_name"),
    ]
    joined = " ".join(str(p) for p in parts if isinstance(p, str) and p.strip())
    return joined.strip()


def parse_reaction_update(update: Mapping[str, Any]) -> InboundReaction | None:
    """Parse 👍/👎 state changes only; never retain who reacted."""
    update_id = update.get("update_id")
    reaction = update.get("message_reaction")
    if not isinstance(update_id, int) or not isinstance(reaction, Mapping):
        return None
    chat = reaction.get("chat")
    chat_id = chat.get("id") if isinstance(chat, Mapping) else None
    message_id = reaction.get("message_id")
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return None
    return InboundReaction(
        update_id=update_id,
        chat_id=chat_id,
        message_id=message_id,
        old_vote=_reaction_vote(reaction.get("old_reaction")),
        new_vote=_reaction_vote(reaction.get("new_reaction")),
    )


def _reaction_vote(value: Any) -> str | None:
    """Map a user's current reaction set to one explicit vote."""
    if not isinstance(value, list):
        return None
    emojis = {
        item.get("emoji")
        for item in value
        if isinstance(item, Mapping) and item.get("type") == "emoji"
    }
    positive = "👍" in emojis
    negative = "👎" in emojis
    if positive == negative:
        return None
    return "up" if positive else "down"
