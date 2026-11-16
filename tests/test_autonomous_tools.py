"""Contract tests for the scoped tools an autonomous agent may call.

These are deliberately about *boundaries*, not about behaviour quality:
the catalog must expose only the eleven scoped tools, must reject malformed
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
from app.memory import (
    MAX_MEMORY_ID_CHARS,
    MAX_MEMORY_PEOPLE,
    MAX_MEMORY_SOURCES,
    MAX_MEMORY_SUMMARY_CHARS,
    MAX_MEMORY_TAG_CHARS,
    MAX_MEMORY_TAGS,
    MAX_RECALL_QUERY_CHARS,
    MAX_RECALL_RESULTS,
    MEMORY_CERTAINTIES,
    MEMORY_KINDS,
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
        "set_intent", "resolve_intent", "remember", "recall_memories", "read_history", "forget_memory",
    }
    assert len(catalog.names) == 11
    # python_exec and friends must never leak in through this catalog.
    assert catalog.get("python_exec") is None
    schemas = catalog.schemas(workspace="/tmp")
    assert len(schemas) == len(CATALOG_TOOL_NAMES)
    assert all(schema["type"] == "function" for schema in schemas)

def test_commitment_selectors_are_exposed_in_tool_schemas():
    catalog, _ = build()
    schemas = {item["function"]["name"]: item["function"]["parameters"]
               for item in catalog.schemas(workspace="/tmp")}
    for name in ("set_intent", "resolve_intent", "send_message",
                 "wait_for_event", "wait_until"):
        assert {"anchor_message_id", "intent_id"} <= set(schemas[name]["properties"])


@pytest.mark.parametrize("arguments", [
    {"text": "hello"},
    {"text": "hello", "anchor_message_id": 1},
    {"text": "hello", "intent_id": "intent-1"},
    {"text": "hello", "resolves_intent": True, "anchor_message_id": 1},
    {"text": "hello", "resolves_intent": True, "intent_id": "intent-1"},
])
def test_send_message_accepts_a_valid_call_and_forwards_intent(arguments):
    catalog, calls = build()
    receipt, state = invoke(catalog, "send_message", arguments)
    assert state == "accepted"
    assert receipt["status"] == "accepted"
    assert calls == [("send_message", arguments, "act-1", "call-1")]


@pytest.mark.parametrize("arguments", [
    {},
    {"text": ""},
    {"text": "   "},
    {"text": "x" * (MAX_MESSAGE_CHARS + 1)},
    {"text": "hi", "reply_to": 0},
    {"text": "hi", "reply_to": True},
    {"text": "hi", "freshness": "whatever"},
    {"text": "hi", "max_unobserved_events": 5},  # only valid with bounded
    {"text": "hi", "anchor_message_id": 1, "intent_id": "i"},
    {"text": "hi", "anchor_message_id": 0},
    {"text": "hi", "intent_id": ""},
    {"text": "hi", "resolves_intent": True},
    {"text": "hi", "anchor_message_id": None},
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

def test_wait_tools_allow_one_optional_commitment_selector():
    catalog, calls = build()
    assert invoke(catalog, "wait_for_event", {
        "purpose": "wait for Alice", "anchor_message_id": 41,
    })[1] == "accepted"
    assert invoke(catalog, "wait_for_event", {
        "purpose": "wait for Alice", "anchor_message_id": 41, "intent_id": "i",
    })[1] == "rejected"
    assert invoke(catalog, "wait_until", {
        "at": "2026-10-28T18:00:00+08:00", "intent_id": "i",
    })[1] == "accepted"
    assert len(calls) == 2


def test_wait_for_event_accepts_only_a_positive_integer_sender_id():
    catalog, calls = build()
    assert invoke(catalog, "wait_for_event", {
        "purpose": "wait for Alice", "sender_id": 123,
    })[1] == "accepted"
    for sender_id in (0, -1, True, "123"):
        assert invoke(catalog, "wait_for_event", {
            "purpose": "wait for Alice", "sender_id": sender_id,
        })[1] == "rejected"
    assert len(calls) == 1


def test_wait_for_event_schema_names_telegram_observation_kinds():
    catalog, _ = build()
    schema = next(
        item for item in catalog.schemas(workspace="/tmp")
        if item["function"]["name"] == "wait_for_event"
    )
    description = schema["function"]["parameters"]["properties"]["event_types"]["description"]
    assert "message" in description
    assert "message_edit" in description
    assert "runtime.observation" in description


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


@pytest.mark.parametrize("kind", MEMORY_KINDS)
@pytest.mark.parametrize("certainty", MEMORY_CERTAINTIES)
def test_remember_forwards_sourced_memory_without_changing_the_payload(kind, certainty):
    catalog, calls = build()
    arguments = {
        "summary": "Alice shared a trail she enjoys; I want to revisit it.",
        "kind": kind,
        "source_sequences": [1, 8],
        "person_ids": [42],
        "tags": ["hiking"],
        "certainty": certainty,
        "importance": 5,
        "expires_at": "2030-01-01T00:00:00+08:00",
    }
    assert invoke(catalog, "remember", arguments)[1] == "accepted"
    assert calls == [("remember", arguments, "act-1", "call-1")]


@pytest.mark.parametrize("overrides", [
    {},
    {"person_ids": [], "tags": [], "importance": 1},
    {"memory_id": "memory-1", "expected_revision": 1},
    {"memory_id": "m" * MAX_MEMORY_ID_CHARS, "expected_revision": 100},
    {"summary": "s" * MAX_MEMORY_SUMMARY_CHARS,
     "source_sequences": list(range(1, MAX_MEMORY_SOURCES + 1)),
     "person_ids": list(range(1, MAX_MEMORY_PEOPLE + 1)),
     "tags": ["t" * MAX_MEMORY_TAG_CHARS] * MAX_MEMORY_TAGS},
])
def test_remember_accepts_create_revision_and_collection_boundaries(overrides):
    catalog, calls = build()
    arguments = {"summary": "We explored a trail", "kind": "experience", "source_sequences": [1],
                 **overrides}
    assert invoke(catalog, "remember", arguments)[1] == "accepted"
    assert calls == [("remember", arguments, "act-1", "call-1")]


@pytest.mark.parametrize("field", ["summary", "kind", "source_sequences"])
def test_remember_requires_content_kind_and_evidence(field):
    catalog, calls = build()
    arguments = {"summary": "A shared trail", "kind": "experience", "source_sequences": [1]}
    del arguments[field]
    receipt, state = invoke(catalog, "remember", arguments)
    assert state == "rejected"
    assert receipt["reason"] == "invalid_payload"
    assert calls == []


@pytest.mark.parametrize("overrides", [
    {"summary": ""}, {"summary": "  "}, {"summary": None},
    {"summary": "s" * (MAX_MEMORY_SUMMARY_CHARS + 1)},
    {"kind": "relationship_score"}, {"kind": None}, {"kind": ["person"]},
    {"kind": "person"}, {"kind": "person", "person_ids": []},
    {"source_sequences": []}, {"source_sequences": [1, 1]},
    {"source_sequences": [True]}, {"source_sequences": [0]},
    {"source_sequences": [-1]}, {"source_sequences": [1.0]},
    {"source_sequences": ["1"]}, {"source_sequences": [{}]},
    {"source_sequences": (1,)}, {"source_sequences": None},
    {"source_sequences": list(range(1, MAX_MEMORY_SOURCES + 2))},
    {"memory_id": "memory-1"}, {"expected_revision": 1},
    {"memory_id": "", "expected_revision": 1},
    {"memory_id": " ", "expected_revision": 1},
    {"memory_id": "m" * (MAX_MEMORY_ID_CHARS + 1), "expected_revision": 1},
    {"memory_id": None, "expected_revision": 1},
    {"memory_id": "m", "expected_revision": True},
    {"memory_id": "m", "expected_revision": 0},
    {"memory_id": "m", "expected_revision": -1},
    {"memory_id": "m", "expected_revision": 1.0},
    {"memory_id": "m", "expected_revision": "1"},
    {"memory_id": "m", "expected_revision": None},
    {"person_ids": [1, 1]}, {"person_ids": [False]}, {"person_ids": [0]},
    {"person_ids": [-1]}, {"person_ids": [1.0]}, {"person_ids": ["1"]},
    {"person_ids": None}, {"person_ids": list(range(1, MAX_MEMORY_PEOPLE + 2))},
    {"tags": [""]}, {"tags": ["  "]}, {"tags": [1]}, {"tags": None},
    {"tags": ["t" * (MAX_MEMORY_TAG_CHARS + 1)]},
    {"tags": ["t"] * (MAX_MEMORY_TAGS + 1)},
    {"certainty": "confirmed"}, {"certainty": None},
    {"importance": True}, {"importance": 0}, {"importance": 6},
    {"importance": 2.0}, {"importance": "2"}, {"importance": None},
    {"expires_at": "2030-01-01T00:00:00"}, {"expires_at": "later"},
    {"expires_at": None}, {"expires_at": 1_900_000_000},
    {"memory_revision": 1}, {"unexpected": 1},
])
def test_remember_rejects_malformed_arguments_before_the_handler(overrides):
    catalog, calls = build()
    arguments = {"summary": "A shared trail", "kind": "experience", "source_sequences": [1],
                 **overrides}
    receipt, state = invoke(catalog, "remember", arguments)
    assert state == "rejected"
    assert receipt == {"status": "rejected", "reason": "invalid_payload", "executed": False}
    assert calls == []


@pytest.mark.parametrize("name,arguments", [
    ("recall_memories", {}),
    ("recall_memories", {"query": "q" * MAX_RECALL_QUERY_CHARS, "person_id": 42,
                         "kind": "person", "tags": ["hiking"], "memory_id": "m",
                         "source_message_id": 123, "limit": MAX_RECALL_RESULTS}),
    ("recall_memories", {"tags": ["t" * MAX_MEMORY_TAG_CHARS] * MAX_MEMORY_TAGS, "limit": 1}),
    ("read_history", {}),
    ("read_history", {"query": "q" * MAX_RECALL_QUERY_CHARS, "person_id": 42,
                      "through_sequence": 8, "message_id": 123, "limit": MAX_RECALL_RESULTS}),
    ("read_history", {"through_sequence": 1, "limit": 1}),
    ("forget_memory", {"memory_id": "m", "expected_revision": 1}),
    ("forget_memory", {"memory_id": "m" * MAX_MEMORY_ID_CHARS, "expected_revision": 2,
                       "reason": "r" * 500}),
])
def test_memory_reads_and_retirement_forward_exact_payloads(name, arguments):
    catalog, calls = build()
    receipt, state = invoke(catalog, name, arguments)
    assert state == "accepted"
    assert receipt["status"] == "accepted"
    assert calls == [(name, arguments, "act-1", "call-1")]


@pytest.mark.parametrize("name", ["recall_memories", "read_history"])
@pytest.mark.parametrize("arguments", [
    {"query": ""}, {"query": "  "}, {"query": None}, {"query": 1},
    {"query": "q" * (MAX_RECALL_QUERY_CHARS + 1)},
    {"person_id": 0}, {"person_id": -1}, {"person_id": True},
    {"person_id": "1"}, {"person_id": 1.0}, {"person_id": None},
    {"limit": 0}, {"limit": MAX_RECALL_RESULTS + 1}, {"limit": True},
    {"limit": "5"}, {"limit": 5.0}, {"limit": None},
    {"unexpected": 1},
])
def test_memory_reads_reject_invalid_shared_filters(name, arguments):
    catalog, calls = build()
    receipt, state = invoke(catalog, name, arguments)
    assert state == "rejected"
    assert receipt["reason"] == "invalid_payload"
    assert calls == []


@pytest.mark.parametrize("name,arguments", [
    ("recall_memories", {"kind": "unknown"}), ("recall_memories", {"kind": None}),
    ("recall_memories", {"tags": []}), ("recall_memories", {"tags": [" "]}),
    ("recall_memories", {"tags": [1]}), ("recall_memories", {"tags": None}),
    ("recall_memories", {"tags": ["t"] * (MAX_MEMORY_TAGS + 1)}),
    ("recall_memories", {"tags": ["t" * (MAX_MEMORY_TAG_CHARS + 1)]}),
    ("recall_memories", {"memory_id": ""}), ("recall_memories", {"memory_id": None}),
    ("recall_memories", {"memory_id": "m" * (MAX_MEMORY_ID_CHARS + 1)}),
    ("recall_memories", {"expected_revision": 1}),
    ("recall_memories", {"message_id": 1}),
    ("read_history", {"source_message_id": 1}),
    ("read_history", {"memory_id": "m"}),
    ("forget_memory", {}),
    ("forget_memory", {"memory_id": "m"}),
    ("forget_memory", {"expected_revision": 1}),
    ("forget_memory", {"memory_id": "", "expected_revision": 1}),
    ("forget_memory", {"memory_id": " ", "expected_revision": 1}),
    ("forget_memory", {"memory_id": None, "expected_revision": 1}),
    ("forget_memory", {"memory_id": "m" * (MAX_MEMORY_ID_CHARS + 1), "expected_revision": 1}),
    ("forget_memory", {"memory_id": "m", "expected_revision": 1, "reason": ""}),
    ("forget_memory", {"memory_id": "m", "expected_revision": 1, "reason": "  "}),
    ("forget_memory", {"memory_id": "m", "expected_revision": 1, "reason": None}),
    ("forget_memory", {"memory_id": "m", "expected_revision": 1, "reason": "r" * 501}),
    ("forget_memory", {"memory_id": "m", "expected_revision": 1, "unexpected": 1}),
])
def test_memory_reads_and_retirement_reject_invalid_specific_fields(name, arguments):
    catalog, calls = build()
    receipt, state = invoke(catalog, name, arguments)
    assert state == "rejected"
    assert receipt["executed"] is False
    assert calls == []


@pytest.mark.parametrize("name,field,base", [
    ("recall_memories", "source_message_id", {}),
    ("read_history", "message_id", {}),
    ("read_history", "through_sequence", {}),
    ("forget_memory", "expected_revision", {"memory_id": "m"}),
])
@pytest.mark.parametrize("value", [0, -1, True, "1", 1.0, None])
def test_memory_source_and_revision_selectors_require_positive_integers(name, field, base, value):
    catalog, calls = build()
    receipt, state = invoke(catalog, name, {**base, field: value})
    assert state == "rejected"
    assert receipt["reason"] == "invalid_payload"
    assert calls == []


@pytest.mark.parametrize("name,base", [
    ("send_message", {"text": "Remember our trail"}),
    ("wait_for_event", {"purpose": "hear about the next hike"}),
    ("wait_until", {"at": "2030-01-01T00:00:00Z"}),
])
@pytest.mark.parametrize("selector", [{}, {"intent_id": "i"}, {"anchor_message_id": 41}])
def test_memory_links_are_independent_of_commitment_selectors(name, base, selector):
    catalog, calls = build()
    arguments = {**base, **selector, "memory_id": "m"}
    if name == "send_message":
        arguments["memory_revision"] = 1
    assert invoke(catalog, name, arguments)[1] == "accepted"
    assert calls == [(name, arguments, "act-1", "call-1")]


@pytest.mark.parametrize("name,base", [
    ("send_message", {"text": "A trail", "memory_revision": 1}),
    ("wait_for_event", {"purpose": "A trail"}),
    ("wait_until", {"at": "2030-01-01T00:00:00Z"}),
])
@pytest.mark.parametrize("memory_id", ["", "  ", None, True, 1, "m" * (MAX_MEMORY_ID_CHARS + 1)])
def test_memory_link_ids_are_validated_before_the_handler(name, base, memory_id):
    catalog, calls = build()
    assert invoke(catalog, name, {**base, "memory_id": memory_id})[1] == "rejected"
    assert calls == []


@pytest.mark.parametrize("arguments", [
    {"memory_id": "m"}, {"memory_revision": 1},
    {"memory_id": "m", "memory_revision": 0},
    {"memory_id": "m", "memory_revision": -1},
    {"memory_id": "m", "memory_revision": True},
    {"memory_id": "m", "memory_revision": 1.0},
    {"memory_id": "m", "memory_revision": "1"},
    {"memory_id": "m", "memory_revision": None},
    {"memory_id": "m", "memory_revision": 1, "resolves_intent": True},
    {"memory_id": "m", "memory_revision": 1, "anchor_message_id": 1, "intent_id": "i"},
])
def test_send_requires_a_memory_revision_pair_and_an_independent_intent_to_resolve(arguments):
    catalog, calls = build()
    assert invoke(catalog, "send_message", {"text": "A trail", **arguments})[1] == "rejected"
    assert calls == []


def test_memory_backed_send_can_resolve_a_selected_intent_independently():
    catalog, calls = build()
    arguments = {"text": "A trail", "memory_id": "m", "memory_revision": 2,
                 "intent_id": "i", "resolves_intent": True}
    assert invoke(catalog, "send_message", arguments)[1] == "accepted"
    assert calls == [("send_message", arguments, "act-1", "call-1")]


@pytest.mark.parametrize("name,base", [
    ("remember", {"summary": "A trail", "kind": "experience", "source_sequences": [1]}),
    ("recall_memories", {}),
    ("read_history", {}),
    ("forget_memory", {"memory_id": "m", "expected_revision": 1}),
])
@pytest.mark.parametrize("field,value", [
    ("chat_id", 123), ("topic_id", 456), ("observed_through", 999),
])
def test_memory_tools_never_accept_model_supplied_scope_or_watermarks(name, base, field, value):
    catalog, calls = build()
    assert invoke(catalog, name, {**base, field: value})[1] == "rejected"
    assert calls == []


@pytest.mark.parametrize("name,arguments", [
    ("remember", {"summary": "A trail", "kind": "experience", "source_sequences": [1]}),
    ("recall_memories", {}), ("read_history", {}),
    ("forget_memory", {"memory_id": "m", "expected_revision": 1}),
])
def test_memory_tools_fail_closed_without_an_application_handler(name, arguments):
    receipt, state = invoke(AutonomousToolCatalog(), name, arguments)
    assert state == "rejected"
    assert receipt == {"status": "rejected", "reason": "no_handler", "executed": False}


def test_read_receipt_returns_application_evidence_unchanged():
    expected = {"status": "accepted", "memories": [{"memory_id": "m", "revision": 2,
                "certainty": "inferred", "needs_review": True, "source_sequences": [8]}]}

    async def read_handler(name, arguments, context, activation, call):
        assert name == "recall_memories"
        return expected, "accepted"

    catalog, _ = build(read_handler)
    receipt, state = invoke(catalog, "recall_memories", {"memory_id": "m"})
    assert state == "accepted"
    assert receipt == expected
