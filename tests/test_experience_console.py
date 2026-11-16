"""Operator memory/history commands show scoped evidence and revision history."""
from __future__ import annotations

import io
from types import SimpleNamespace

from app import fixtures
from app.config import AppConfig
from app.console import Console
from app.store import Store, StoredMessage
from conftest import FakeClock


def source(store, identity, text, *, topic=fixtures.TOPIC_A, message_id=None):
    store.record_inbound_message(
        StoredMessage(fixtures.TEST_CHAT, message_id or identity, topic, fixtures.ALICE,
                      "alice", False, text, 1_700_000_000.0),
        update_id=identity, payload={},
    )
    return int(store._query("SELECT sequence FROM observation_events WHERE update_id=?", (identity,))[0]["sequence"])


def remember(store, identity, text, *, topic=fixtures.TOPIC_A, importance=2):
    sequence = source(store, identity, text, topic=topic)
    return store.remember_memory(
        memory_id=f"memory-{identity}", chat_id=fixtures.TEST_CHAT, topic_id=topic,
        summary=text, kind="person", source_sequences=[sequence], observed_through=sequence,
        person_ids=[fixtures.ALICE], certainty="observed", importance=importance,
    )


def console_for(store):
    stream = io.StringIO()
    console = Console(
        bot=SimpleNamespace(store=store, chat_kinds={fixtures.TEST_CHAT: "supergroup"}),
        config=AppConfig(), stream=stream,
    )
    return console, stream


def command(console, stream, text):
    start = stream.tell()
    console.handle(text)
    return stream.getvalue()[start:]


def test_memory_list_details_and_history_respect_watched_topic_before_limit(tmp_path):
    store = Store(tmp_path / "console.sqlite3", clock=FakeClock())
    try:
        foreign = remember(store, 101, "Bob's other-topic detail", topic=fixtures.TOPIC_B, importance=5)
        current = remember(store, 102, "Alice likes concise answers", importance=1)
        console, stream = console_for(store)
        command(console, stream, f"watch {fixtures.TEST_CHAT} {fixtures.TOPIC_A}")
        listing = command(console, stream, "memories 1")
        assert current["summary"] in listing and foreign["summary"] not in listing
        assert current["memory_id"] in listing and foreign["memory_id"] not in listing
        details = command(console, stream, f"memory {current['memory_id']}")
        assert current["summary"] in details
        assert f"seq={current['source_sequences'][0]}" in details
        assert str(fixtures.ALICE) in details
        denied = command(console, stream, f"memory {foreign['memory_id']}")
        assert foreign["summary"] not in denied
        history = command(console, stream, "history 1")
        assert current["summary"] in history and foreign["summary"] not in history
    finally:
        store.close()


def test_memory_details_keep_corrections_and_retirement_inspectable(tmp_path):
    store = Store(tmp_path / "console.sqlite3", clock=FakeClock())
    try:
        original = remember(store, 201, "Alice uses UTC")
        edited = source(store, 202, "Alice uses Asia/Shanghai", message_id=201)
        current = store.remember_memory(
            memory_id=original["memory_id"], expected_revision=1,
            chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A,
            summary="Alice uses Asia/Shanghai", kind="person",
            source_sequences=[edited], observed_through=edited,
            person_ids=[fixtures.ALICE], certainty="observed",
        )
        console, stream = console_for(store)
        command(console, stream, f"watch {fixtures.TEST_CHAT} {fixtures.TOPIC_A}")
        details = command(console, stream, f"memory {current['memory_id']}")
        assert original["summary"] in details and current["summary"] in details
        assert f"seq={edited}" in details
        history = command(console, stream, "history")
        assert original["summary"] in history and current["summary"] in history
        store.forget_memory(
            current["memory_id"], expected_revision=2,
            chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A,
        )
        assert current["summary"] not in command(console, stream, "memories")
        retired = command(console, stream, f"memory {current['memory_id']}")
        assert "forgotten" in retired and original["summary"] in retired
    finally:
        store.close()


def test_memory_commands_require_an_explicit_scope(tmp_path):
    store = Store(tmp_path / "console.sqlite3", clock=FakeClock())
    try:
        memory = remember(store, 301, "An experience requiring a selected scope")
        console, stream = console_for(store)
        for text in ("memories", f"memory {memory['memory_id']}", "history"):
            assert memory["summary"] not in command(console, stream, text)
        command(console, stream, f"watch {fixtures.TEST_CHAT} {fixtures.TOPIC_A}")
        assert memory["summary"] in command(console, stream, "memories")
    finally:
        store.close()
