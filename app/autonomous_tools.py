"""The scoped action tools an autonomous group agent may call.

Design rule: these tools carry *intent*, not effects. Each one validates its
arguments and hands them to an injected async handler that belongs to the
application. Nothing here talks to Telegram, writes to SQLite, or performs a
network request. That keeps the decision ("the agent chose to speak") and the
execution ("the outbox sent it") on separate, separately auditable sides of a
durable boundary.

The tool set is deliberately small and closed:

  send_message     propose one message for this chat/topic
  finish_silently  end this turn with no visible message
  wait_for_event   end this turn and be woken by a future event
  wait_until       end this turn and be woken at a wall-clock time
  update_state     remember something for later turns
  set_intent       record what this turn is trying to accomplish, and stay
                   on it across wakes
  resolve_intent   close that goal, because it is done with

Only `send_message` can create an outbound action, and only the application
can turn an accepted action into a Telegram request. Ordinary assistant text
exposed by the model is never delivered as a group message.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

# Hard bounds. These are admission checks, not policy: the application may
# impose further limits (rate, budget, authorization) when it handles a call.
MAX_MESSAGE_CHARS = 4096
MAX_REASON_CHARS = 500
MAX_PURPOSE_CHARS = 500
MAX_STATE_KEY_CHARS = 100
MAX_STATE_BYTES = 16 * 1024
MAX_EVENT_TYPES = 20
MAX_EVENT_TYPE_CHARS = 100
MAX_STATE_DEPTH = 8

CATALOG_TOOL_NAMES = (
    "send_message",
    "finish_silently",
    "wait_for_event",
    "wait_until",
    "update_state",
    "set_intent",
    "resolve_intent",
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:\-]{1,%d}$" % MAX_EVENT_TYPE_CHARS)

ToolHandler = Callable[
    [str, dict[str, Any], Any, Any, Any],
    Awaitable[tuple[dict[str, Any], str]],
]


def _nonempty(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(value) > limit:
        return None
    return value


def _json_depth(value: Any, depth: int = 0) -> int:
    """Depth of a JSON-ish value; used to bound update_state payloads."""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            return MAX_STATE_DEPTH + 1
        return 1 + max((_json_depth(item, depth + 1) for item in value.values()), default=0)
    if isinstance(value, (list, tuple)):
        return 1 + max((_json_depth(item, depth + 1) for item in value), default=0)
    return 1


def _valid_json_scalar(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _valid_json_value(value: Any, depth: int = 0) -> bool:
    if depth > MAX_STATE_DEPTH:
        return False
    if _valid_json_scalar(value):
        return True
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _valid_json_value(item, depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_valid_json_value(item, depth + 1) for item in value)
    return False


def _aware_timestamp(value: Any) -> float | None:
    """Parse an ISO-8601 timestamp that carries an explicit timezone.

    A naive timestamp is rejected rather than guessed: "17:00" means different
    instants in different deployment timezones, and silently assuming UTC
    would make a wait fire at the wrong time.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00") if value.strip().endswith("Z") else value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.timestamp()


class _BaseTool:
    name = ""

    def __init__(self, catalog: "AutonomousToolCatalog") -> None:
        self._catalog = catalog

    def schema(self, *, workspace: str) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.__doc__ or "",
            "parameters": self.parameters(),
        }}

    def parameters(self) -> dict[str, Any]:
        raise NotImplementedError

    def validate(self, arguments: dict[str, Any], *, max_timeout: float) -> str | None:
        raise NotImplementedError

    async def invoke(self, arguments: dict[str, Any], *, context: Any,
                     activation: Any, call: Any) -> tuple[dict[str, Any], str]:
        # Every tool funnels through the catalog, so validation and the
        # accepted/rejected receipt shape cannot drift between tools.
        return await self._catalog.dispatch(
            self.name, arguments, context=context, activation=activation, call=call
        )


class _SendMessage(_BaseTool):
    """Send one message to the current chat. This is the only way to speak.

    `freshness` decides what happens when the conversation moves on while the
    reply is being written - in a busy group that is the normal case, not the
    exception. It is described here in full because an unexplained default was
    silently discarding every message she wrote: `strict` cancels the send if
    anything new arrives, and nothing had told the model that.

    `default` is the one to leave alone for ordinary chat: it adapts to how
    fast the conversation is moving instead of demanding a frozen room.
    """

    name = "send_message"

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": MAX_MESSAGE_CHARS},
                "reply_to": {"type": "integer", "minimum": 1},
                "freshness": {
                    "type": "string",
                    "enum": ["default", "strict", "bounded"],
                    "description": (
                        "How much newer conversation may arrive before this reply is "
                        "discarded instead of sent. 'default' (recommended) tolerates "
                        "the trickle of chatter that is normal in an active group. "
                        "'strict' discards the reply if ANY new message arrived - use "
                        "only when being overtaken by a correction would make the "
                        "message wrong. 'bounded' sends only if at most "
                        "max_unobserved_events newer messages arrived."
                    ),
                },
                "max_unobserved_events": {
                    "type": "integer", "minimum": 0, "maximum": 100,
                    "description": (
                        "With freshness='bounded': how many newer messages may arrive "
                        "before this reply is dropped. Ignored otherwise."
                    ),
                },
                "resolves_intent": {
                    "type": "boolean",
                    "description": (
                        "Set true when this message is the thing you were waiting to "
                        "say, so the open goal is closed with it. Leave false if the "
                        "goal continues after saying this."
                    ),
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        }

    def validate(self, arguments: dict[str, Any], *, max_timeout: float) -> str | None:
        if set(arguments) - {"text", "reply_to", "freshness", "max_unobserved_events",
                             "resolves_intent"}:
            return "invalid_payload"
        if "resolves_intent" in arguments and not isinstance(
                arguments["resolves_intent"], bool):
            return "invalid_payload"
        if _nonempty(arguments.get("text"), MAX_MESSAGE_CHARS) is None:
            return "invalid_payload"
        reply_to = arguments.get("reply_to")
        if reply_to is not None and (
            isinstance(reply_to, bool) or not isinstance(reply_to, int) or reply_to < 1
        ):
            return "invalid_payload"
        freshness = arguments.get("freshness")
        if freshness is not None and freshness not in {"default", "strict", "bounded"}:
            return "invalid_payload"
        cap = arguments.get("max_unobserved_events")
        if cap is not None and (
            isinstance(cap, bool) or not isinstance(cap, int) or not 0 <= cap <= 100
        ):
            return "invalid_payload"
        if cap is not None and freshness != "bounded":
            return "invalid_payload"
        return None


class _FinishSilently(_BaseTool):
    """End this turn without sending a message. Use when no reply is useful."""

    name = "finish_silently"

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"reason": {"type": "string", "maxLength": MAX_REASON_CHARS}},
            "additionalProperties": False,
        }

    def validate(self, arguments: dict[str, Any], *, max_timeout: float) -> str | None:
        if set(arguments) - {"reason"}:
            return "invalid_payload"
        if "reason" in arguments and _nonempty(arguments["reason"], MAX_REASON_CHARS) is None:
            return "invalid_payload"
        return None


class _WaitForEvent(_BaseTool):
    """End this turn and wake up when a future event matches."""

    name = "wait_for_event"

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "purpose": {"type": "string", "minLength": 1, "maxLength": MAX_PURPOSE_CHARS},
                "event_types": {
                    "type": "array", "minItems": 1, "maxItems": MAX_EVENT_TYPES,
                    "items": {"type": "string", "maxLength": MAX_EVENT_TYPE_CHARS},
                },
                "expires_at": {"type": "string", "maxLength": 64},
            },
            "required": ["purpose"],
            "additionalProperties": False,
        }

    def validate(self, arguments: dict[str, Any], *, max_timeout: float) -> str | None:
        if set(arguments) - {"purpose", "event_types", "expires_at"}:
            return "invalid_payload"
        if _nonempty(arguments.get("purpose"), MAX_PURPOSE_CHARS) is None:
            return "invalid_payload"
        types = arguments.get("event_types")
        if types is not None:
            if not isinstance(types, list) or not 1 <= len(types) <= MAX_EVENT_TYPES:
                return "invalid_payload"
            if len(set(map(str, types))) != len(types):
                return "invalid_payload"
            if any(_nonempty(item, MAX_EVENT_TYPE_CHARS) is None for item in types):
                return "invalid_payload"
            if any(not _IDENTIFIER.match(item.strip()) for item in types):
                return "invalid_payload"
        if "expires_at" in arguments and _aware_timestamp(arguments["expires_at"]) is None:
            return "invalid_payload"
        return None


class _WaitUntil(_BaseTool):
    """End this turn and wake up at a specific later time to reconsider."""

    name = "wait_until"

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "at": {"type": "string", "maxLength": 64},
                "purpose": {"type": "string", "maxLength": MAX_PURPOSE_CHARS},
                "expires_at": {"type": "string", "maxLength": 64},
            },
            "required": ["at"],
            "additionalProperties": False,
        }

    def validate(self, arguments: dict[str, Any], *, max_timeout: float) -> str | None:
        if set(arguments) - {"at", "purpose", "expires_at"}:
            return "invalid_payload"
        if _aware_timestamp(arguments.get("at")) is None:
            return "invalid_payload"
        if "purpose" in arguments and _nonempty(arguments["purpose"], MAX_PURPOSE_CHARS) is None:
            return "invalid_payload"
        if "expires_at" in arguments:
            expiry = _aware_timestamp(arguments["expires_at"])
            if expiry is None or expiry < (_aware_timestamp(arguments["at"]) or 0):
                return "invalid_payload"
        return None


class _UpdateState(_BaseTool):
    """Remember a small, bounded note for later turns in this chat."""

    name = "update_state"

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {"type": "string", "minLength": 1, "maxLength": MAX_STATE_KEY_CHARS},
                "value": {},
                "expires_at": {"type": "string", "maxLength": 64},
            },
            "required": ["key", "value"],
            "additionalProperties": False,
        }

    def validate(self, arguments: dict[str, Any], *, max_timeout: float) -> str | None:
        import json

        if set(arguments) - {"key", "value", "expires_at"}:
            return "invalid_payload"
        if _nonempty(arguments.get("key"), MAX_STATE_KEY_CHARS) is None:
            return "invalid_payload"
        if "value" not in arguments:
            return "invalid_payload"
        value = arguments["value"]
        if not _valid_json_value(value):
            return "invalid_payload"
        try:
            size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            return "invalid_payload"
        if size > MAX_STATE_BYTES:
            return "invalid_payload"
        if "expires_at" in arguments and _aware_timestamp(arguments["expires_at"]) is None:
            return "invalid_payload"
        return None


class _SetIntent(_BaseTool):
    """Record what you are trying to accomplish here, so a later wake continues it.

    A wake is not a fresh conversation: you are the same participant, and this
    is how that survives the gap. Write the goal as something you could act on
    when you are woken - what you are waiting for, and what you would do about
    it - not as a status report.

    One goal per chat (or per topic). Calling this again replaces the current
    goal rather than adding a second one.
    """

    name = "set_intent"

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "minLength": 1, "maxLength": 1000,
                         "description": "What you are trying to accomplish."},
                "expires_at": {"type": "string", "maxLength": 64,
                               "description": "ISO-8601 with timezone. Optional."},
            },
            "required": ["goal"],
            "additionalProperties": False,
        }

    def validate(self, arguments: dict[str, Any], *, max_timeout: float) -> str | None:
        if set(arguments) - {"goal", "expires_at"}:
            return "invalid_payload"
        if _nonempty(arguments.get("goal"), 1000) is None:
            return "invalid_payload"
        if "expires_at" in arguments and _aware_timestamp(arguments["expires_at"]) is None:
            return "invalid_payload"
        return None


class _ResolveIntent(_BaseTool):
    """Close the current goal. Use when it is genuinely finished, or dropped.

    Closing is not a report to anyone: nobody is told. Say so with
    `send_message` if the chat should know.
    """

    name = "resolve_intent"

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "maxLength": MAX_REASON_CHARS},
                "abandoned": {
                    "type": "boolean",
                    "description": (
                        "True if you are dropping the goal rather than completing it. "
                        "The difference is recorded, nothing else changes."
                    ),
                },
            },
            "additionalProperties": False,
        }

    def validate(self, arguments: dict[str, Any], *, max_timeout: float) -> str | None:
        if set(arguments) - {"reason", "abandoned"}:
            return "invalid_payload"
        if "reason" in arguments and _nonempty(arguments["reason"], MAX_REASON_CHARS) is None:
            return "invalid_payload"
        if "abandoned" in arguments and not isinstance(arguments["abandoned"], bool):
            return "invalid_payload"
        return None


class AutonomousToolCatalog:
    """Exposes only the scoped action tools above.

    `handler` is provided by the application and owns every effect. Returning
    "accepted" means the application durably recorded the intent - it does not
    mean Telegram delivered anything, and the tool receipt says so explicitly.
    """

    def __init__(self, handler: ToolHandler | None = None) -> None:
        self._handler = handler
        self._tools: dict[str, _BaseTool] = {
            tool.name: tool
            for tool in (_SendMessage(self), _FinishSilently(self), _WaitForEvent(self),
                         _WaitUntil(self), _UpdateState(self), _SetIntent(self),
                         _ResolveIntent(self))
        }

    def bind(self, handler: ToolHandler) -> None:
        """Point the catalog at the handler for the current workflow.

        A runtime is single-threaded per scope and processes one activation at
        a time, so rebinding between turns is safe; it also keeps the handler
        - which knows the workflow id and observation watermark - out of the
        runtime's constructor.
        """
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handler = handler

    def unbind(self) -> None:
        self._handler = None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def schemas(self, *, workspace: str | None = None) -> list[dict[str, Any]]:
        return [tool.schema(workspace=workspace or "") for tool in self._tools.values()]

    def get(self, name: str) -> _BaseTool | None:
        return self._tools.get(name)

    async def dispatch(self, name: str, arguments: dict[str, Any], *,
                       context: Any, activation: Any, call: Any) -> tuple[dict[str, Any], str]:
        """Validate and forward one call. Used by tests and by the tool wrapper."""
        tool = self._tools.get(name)
        if tool is None:
            return {"status": "rejected", "reason": "unknown_tool", "executed": False}, "rejected"
        reason = tool.validate(arguments, max_timeout=getattr(context, "max_timeout", 0) or 0)
        if reason is not None:
            return {"status": "rejected", "reason": reason, "executed": False}, "rejected"
        if self._handler is None:
            # Fail closed. A catalog with no sink must not pretend to accept.
            return ({"status": "rejected", "reason": "no_handler", "executed": False},
                    "rejected")
        receipt, state = await self._handler(name, dict(arguments), context, activation, call)
        return dict(receipt), state
