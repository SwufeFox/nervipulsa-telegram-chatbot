"""Contract tests for the scoped tools an autonomous agent may call.

These are deliberately about *boundaries*, not about behaviour quality:
the catalog must expose only the five scoped tools, must reject malformed
arguments before the application sees them, and must never perform a side
effect of its own.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.autonomous_tools import (
    CATALOG_TOOL_NAMES,
    MAX_MESSAGE_CHARS,
    AutonomousToolCatalog,
)


class FakeContext:
    max_timeout = 30


def build(handler=None):
    calls: list[tuple] = []

    async def default(name, arguments, context, activation, call):
        calls.append((name, arguments, activation.id, call.id))
        return {"status": "accepted", "action_id": f"a-{len(calls)}"}, "accepted"

    catalog = AutonomousToolCatalog(handler or default)
    return catalog, calls


def tool(name):
    return SimpleNamespace(id="call-1", name=name, arguments={})


def invoke(catalog, name, arguments):
    return asyncio.run(catalog.dispatch(
        name, arguments, context=FakeContext(),
        activation=SimpleNamespace(id="act-1"), call=tool(name),
    ))


def test_catalog_exposes_only_the_scoped_tools():
    catalog, _ = build()
    assert catalog.names == CATALOG_TOOL_NAMES
    assert set(name for name in catalog.names) == {
        "send_message", "finish_silently", "wait_for_event", "wait_until", "update_state",
        "set_intent", "resolve_intent",
    }
    # python_exec and friends must never leak in through this catalog.
    assert catalog.get("python_exec") is None
    schemas = catalog.schemas(workspace="/tmp")
    assert len(schemas) == len(CATALOG_TOOL_NAMES)
    assert all(schema["type"] == "function" for schema in schemas)


def test_send_message_accepts_a_valid_call_and_forwards_intent():
    catalog, calls = build()
    receipt, state = invoke(catalog, "send_message", {"text": "hello"})
    assert state == "accepted"
    assert receipt["status"] == "accepted"
    assert calls == [("send_message", {"text": "hello"}, "act-1", "call-1")]


@pytest.mark.parametrize("arguments", [
    {},
    {"text": ""},
    {"text": "   "},
    {"text": "x" * (MAX_MESSAGE_CHARS + 1)},
    {"text": "hi", "reply_to": 0},
    {"text": "hi", "reply_to": True},
    {"text": "hi", "freshness": "whatever"},
    {"text": "hi", "max_unobserved_events": 5},  # only valid with bounded
    {"text": "hi", "unexpected": 1},
])
def test_send_message_rejects_malformed_arguments_before_the_handler(arguments):
    catalog, calls = build()
    receipt, state = invoke(catalog, "send_message", arguments)
    assert state == "rejected"
    assert receipt["executed"] is False
    assert calls == [], "the application must not see an invalid action"


def test_finish_silently_needs_no_arguments_but_bounds_the_reason():
    catalog, calls = build()
    assert invoke(catalog, "finish_silently", {})[1] == "accepted"
    assert invoke(catalog, "finish_silently", {"reason": "already answered"})[1] == "accepted"
    assert invoke(catalog, "finish_silently", {"reason": "x" * 501})[1] == "rejected"
    assert len(calls) == 2


def test_wait_tools_require_timezone_aware_timestamps():
    catalog, calls = build()
    assert invoke(catalog, "wait_until", {"at": "2026-10-28T18:00:00"})[1] == "rejected"
    assert invoke(catalog, "wait_until", {"at": "2026-10-28T18:00:00+08:00"})[1] == "accepted"
    assert invoke(catalog, "wait_for_event", {"purpose": "see the fix"})[1] == "accepted"
    assert invoke(catalog, "wait_for_event", {
        "purpose": "see the fix", "event_types": ["message", "message"],
    })[1] == "rejected"
    assert len(calls) == 2


def test_update_state_rejects_oversized_or_non_json_values():
    catalog, calls = build()
    assert invoke(catalog, "update_state", {"key": "topic", "value": {"a": [1, 2]}})[1] == "accepted"
    assert invoke(catalog, "update_state", {"key": "topic", "value": float("nan")})[1] == "rejected"
    assert invoke(catalog, "update_state", {"key": "topic", "value": object()})[1] == "rejected"
    assert invoke(catalog, "update_state", {"key": "topic", "value": "x" * 20_000})[1] == "rejected"
    assert len(calls) == 1


def test_unknown_tool_is_rejected_without_calling_the_handler():
    catalog, calls = build()
    receipt, state = invoke(catalog, "python_exec", {"code": "print(1)"})
    assert state == "rejected"
    assert receipt["reason"] == "unknown_tool"
    assert calls == []


def test_handler_exception_is_not_swallowed_into_a_false_acceptance():
    async def boom(*_args):
        raise RuntimeError("outbox unavailable")

    catalog, _ = build(boom)
    with pytest.raises(RuntimeError):
        invoke(catalog, "send_message", {"text": "hi"})
