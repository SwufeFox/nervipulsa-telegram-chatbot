"""Telegram long polling and send. No framework, no event loop magic.

Two things this module is careful about:

**Update parsing** is total. Anything it does not understand becomes `None`
rather than an exception, because a bot that dies on a service message is a bot
that loses its poll cursor.

**Send outcomes are three-valued.** A send either succeeded (we hold a message
id), was definitively rejected by Telegram (HTTP 4xx with a parseable error), or
is *unknown* (timeout, connection reset, 5xx, a process that died mid-request).
Collapsing unknown into failure is how a bot posts the same message twice.

The transport is injectable so failure experiments can substitute one.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Mapping, Protocol

API_ROOT = "https://api.telegram.org"


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


class Transport(Protocol):
    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class TelegramTransport:
    """urllib, one request per call. Raises `TransportError` with a classification."""

    def __init__(self, token: str, *, timeout: float = 30.0) -> None:
        if not token:
            raise ValueError("telegram token is required")
        self._token = token
        self._timeout = timeout

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{API_ROOT}/bot{self._token}/{method}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            raise TransportError(
                kind="rejected", status=exc.code, detail=detail
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            # The request may or may not have reached Telegram.
            raise TransportError(kind="indeterminate", detail=str(exc)[:300]) from exc
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise TransportError(kind="indeterminate", detail="unparseable response") from exc
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            description = ""
            if isinstance(parsed, dict):
                description = str(parsed.get("description") or "")
            raise TransportError(kind="rejected", detail=description[:300])
        return parsed


class TransportError(Exception):
    def __init__(self, *, kind: str, detail: str, status: int | None = None) -> None:
        super().__init__(detail)
        self.kind = kind            # "rejected" | "indeterminate"
        self.detail = detail
        self.status = status


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return str(parsed.get("description") or body)[:300]
        return body[:300]
    except Exception:
        return f"HTTP {exc.code}"


class TelegramClient:
    """Thin wrapper over the Bot API surface this application actually uses."""

    def __init__(self, transport: Transport, *, dry_run: bool = False) -> None:
        self._transport = transport
        self.dry_run = dry_run
        self._dry_run_counter = 0

    # -- inbound -----------------------------------------------------------

    def get_updates(self, *, offset: int | None, timeout: int = 25,
                    limit: int = 50) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "limit": limit,
            "allowed_updates": ["message", "edited_message"],
        }
        if offset is not None:
            payload["offset"] = offset
        response = self._transport.call("getUpdates", payload)
        result = response.get("result")
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    # -- outbound ----------------------------------------------------------

    def send_message(
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
            response = self._transport.call("sendMessage", payload)
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

    def get_me(self) -> dict[str, Any]:
        return self._transport.call("getMe", {}).get("result") or {}

    def delete_message(self, *, chat_id: int, message_id: int) -> bool:
        try:
            self._transport.call(
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
