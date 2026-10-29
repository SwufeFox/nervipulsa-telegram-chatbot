"""A goal survives the gap between activations.

The gap is the whole point. A workflow is one model activation, so between two
wakes there is nothing alive that remembers what the previous one was doing -
unless the goal is stored. These tests pin the storage contract: one open goal
per scope, readable on every wake, closed only by an explicit act or a deadline,
and never closed by a failed turn.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app.store import Store


@pytest.fixture()
def store(tmp_path):
    reality = Store(tmp_path / "app.db")
    yield reality
    reality.close()


def test_one_open_goal_per_scope(store):
    """Two activations must not both invent a goal for the same topic."""
    assert store.open_intent(intent_id="i1", chat_id=1, topic_id=7, goal="等日志") is True
    assert store.open_intent(intent_id="i2", chat_id=1, topic_id=7, goal="另一个") is False
    current = store.current_intent(1, 7)
    assert current is not None and current["intent_id"] == "i1"


@pytest.mark.parametrize("topic_id", [None, 7])
def test_unique_open_intent_is_enforced_by_sqlite(store, topic_id):
    assert store.open_intent(intent_id="i1", chat_id=1, topic_id=topic_id, goal="first")
    with pytest.raises(sqlite3.IntegrityError):
        with store._write() as conn:
            conn.execute(
                "INSERT INTO agent_intents (intent_id, chat_id, topic_id, goal, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, 'open', ?, ?)",
                ("i2", 1, topic_id, "second", 1, 1),
            )
    assert store.resolve_intent("i1", how="agent")
    assert store.open_intent(intent_id="i2", chat_id=1, topic_id=topic_id, goal="second")


def test_reopen_adds_null_topic_unique_index(tmp_path):
    path = tmp_path / "app.db"
    first = Store(path)
    assert first.open_intent(intent_id="i1", chat_id=1, topic_id=None, goal="first")
    first._conn.execute("DROP INDEX agent_intents_one_open_no_topic")
    first._conn.commit()
    first.close()

    second = Store(path)
    try:
        assert not second.open_intent(intent_id="i2", chat_id=1, topic_id=None, goal="second")
        assert second.current_intent(1, None)["intent_id"] == "i1"
    finally:
        second.close()


def test_scopes_are_independent(store):
    assert store.open_intent(intent_id="a", chat_id=1, topic_id=7, goal="群 A") is True
    assert store.open_intent(intent_id="b", chat_id=1, topic_id=8, goal="群 B") is True
    assert store.open_intent(intent_id="c", chat_id=1, topic_id=None, goal="私聊") is True
    assert store.current_intent(1, 7)["intent_id"] == "a"
    assert store.current_intent(1, 8)["intent_id"] == "b"
    assert store.current_intent(1, None)["intent_id"] == "c"


def test_a_new_goal_replaces_an_open_one(store):
    store.open_intent(intent_id="i1", chat_id=1, topic_id=7, goal="先看日志")
    assert store.update_intent_goal("i1", "改成看版本号") is True
    assert store.current_intent(1, 7)["goal"] == "改成看版本号"
    assert store._query("SELECT COUNT(*) AS n FROM agent_intents")[0]["n"] == 1


def test_closing_frees_the_slot_and_is_idempotent(store):
    store.open_intent(intent_id="i1", chat_id=1, topic_id=7, goal="一件事")
    assert store.resolve_intent("i1", how="agent") is True
    assert store.resolve_intent("i1", how="agent") is False  # already closed
    assert store.current_intent(1, 7) is None
    # The unique index covers open rows only, so a second goal is allowed now.
    assert store.open_intent(intent_id="i2", chat_id=1, topic_id=7, goal="下一件") is True


def test_abandoned_is_distinguished_from_resolved(store):
    store.open_intent(intent_id="i1", chat_id=1, topic_id=7, goal="算了")
    store.resolve_intent("i1", how="agent", status="abandoned")
    row = store._query("SELECT status, resolved_by FROM agent_intents")[0]
    assert row["status"] == "abandoned"
    assert row["resolved_by"] == "agent"


def test_only_terminal_statuses_are_accepted(store):
    store.open_intent(intent_id="i1", chat_id=1, topic_id=7, goal="x")
    with pytest.raises(ValueError):
        store.resolve_intent("i1", how="agent", status="open")


def test_an_empty_goal_is_refused(store):
    assert store.open_intent(intent_id="i1", chat_id=1, topic_id=7, goal="   ") is False
    assert store.current_intent(1, 7) is None


def test_expiry_closes_and_reports(store):
    store.open_intent(intent_id="i1", chat_id=1, topic_id=7, goal="会过期",
                      expires_at=time.time() - 1)
    store.open_intent(intent_id="i2", chat_id=1, topic_id=8, goal="永不过期")
    expired = store.expire_intents()
    assert [row["intent_id"] for row in expired] == ["i1"]
    by_id = {row["intent_id"]: row for row in
             store._query("SELECT * FROM agent_intents")}
    assert by_id["i1"]["status"] == "abandoned"
    assert by_id["i1"]["resolved_by"] == "expired"
    assert by_id["i2"]["status"] == "open"
    # Running again must not re-report the same goal.
    assert store.expire_intents() == []


def test_the_goal_survives_a_reopen(tmp_path):
    """A restart is exactly the gap this exists to cross."""
    path = tmp_path / "app.db"
    first = Store(path)
    first.open_intent(intent_id="i1", chat_id=1, topic_id=7, goal="跨重启的目标")
    first.close()

    second = Store(path)
    try:
        current = second.current_intent(1, 7)
        assert current is not None
        assert current["goal"] == "跨重启的目标"
    finally:
        second.close()


def test_an_old_database_gains_the_column(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` will not alter an existing table."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE actions (
            action_id TEXT PRIMARY KEY, workflow_id TEXT, chat_id INTEGER NOT NULL,
            topic_id INTEGER, reply_to INTEGER, text TEXT NOT NULL,
            observed_through INTEGER NOT NULL, freshness TEXT NOT NULL DEFAULT 'strict',
            max_unobserved INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            telegram_message_id INTEGER, error TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL
        );
    """)
    conn.commit()
    conn.close()

    opened = Store(path)
    try:
        columns = {row["name"] for row in
                   opened._conn.execute("PRAGMA table_info(actions)").fetchall()}
        assert "intent_id" in columns
        # And the column is actually usable, not merely present.
        opened.prepare_action(
            action_id="a1", workflow_id=None, chat_id=1, topic_id=7, text="hi",
            observed_through=1, intent_id="i1",
        )
        assert opened._query("SELECT intent_id FROM actions")[0]["intent_id"] == "i1"
    finally:
        opened.close()


def test_null_topic_uniqueness(store):
    assert store.open_intent(intent_id="a", chat_id=1, topic_id=None, goal="one")
    assert not store.open_intent(intent_id="b", chat_id=1, topic_id=None, goal="two")


def test_delivery_of_old_goal_does_not_resolve_replacement(store):
    store.replace_intent(intent_id="old", chat_id=1, topic_id=None, goal="old", expires_at=None)
    store.prepare_action(action_id="a", workflow_id=None, chat_id=1, topic_id=None,
                         text="answer", observed_through=0, intent_id="old")
    store.replace_intent(intent_id="new", chat_id=1, topic_id=None, goal="new", expires_at=None)
    assert store.claim_action("a") == "claimed"
    assert store.finish_action("a", "sent", telegram_message_id=5)
    assert store.current_intent(1, None)["intent_id"] == "new"


def test_expiry_and_observation_are_durable_together(tmp_path):
    path = tmp_path / "app.db"
    store = Store(path)
    store.open_intent(intent_id="i", chat_id=1, topic_id=None, goal="expired", expires_at=1)
    assert len(store.expire_intents(now=2)) == 1
    store.close()
    store = Store(path)
    try:
        assert store.current_intent(1, None) is None
        assert [row["kind"] for row in store.pending_observations(1, None)] == ["intent_expired"]
        assert store.expire_intents(now=3) == []
    finally:
        store.close()
