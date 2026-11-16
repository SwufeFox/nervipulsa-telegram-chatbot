"""Storage contract for independent, message-anchored commitments."""
from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.store import SCHEMA_VERSION, StoredMessage, Store


@pytest.fixture()
def store(tmp_path):
    value = Store(tmp_path / "app.db")
    yield value
    value.close()


def source(store, chat_id=1, topic_id=None, message_id=1, sender_id=10):
    store.record_message(StoredMessage(
        chat_id, message_id, topic_id, sender_id, f"sender-{sender_id}", False,
        "request", float(message_id)), update_id=message_id)


def open_goal(store, intent_id, anchor, *, chat_id=1, topic_id=None, goal=None, **kw):
    source(store, chat_id, topic_id, anchor, sender_id=anchor + 100)
    return store.open_intent(
        intent_id=intent_id, chat_id=chat_id, topic_id=topic_id,
        goal=goal or f"goal {intent_id}", anchor_message_id=anchor, **kw,
    )


def test_multiple_roots_and_null_topic_anchor_uniqueness(store):
    assert open_goal(store, "left", 11)
    assert open_goal(store, "right", 12)
    assert not store.open_intent(intent_id="duplicate", chat_id=1, topic_id=None,
                                 goal="duplicate", anchor_message_id=11)
    assert [row["intent_id"] for row in store.open_intents(1, None)] == ["left", "right"]
    assert store.intent_for_anchor(1, None, 12)["intent_id"] == "right"


def test_distinct_topics_and_chats_have_independent_anchor_identity(store):
    assert open_goal(store, "topic-a", 1, chat_id=4, topic_id=7)
    assert open_goal(store, "topic-b", 2, chat_id=4, topic_id=8)
    assert open_goal(store, "chat-b", 1, chat_id=5, topic_id=None)
    assert [row["intent_id"] for row in store.open_intents(4, 7)] == ["topic-a"]
    assert [row["intent_id"] for row in store.open_intents(4, 8)] == ["topic-b"]
    assert [row["intent_id"] for row in store.open_intents(5, None)] == ["chat-b"]


def test_concurrent_open_same_anchor_has_one_winner(tmp_path):
    path = tmp_path / "race.db"
    seed = Store(path)
    try:
        source(seed, message_id=9)
    finally:
        seed.close()
    gate = threading.Barrier(2, timeout=10)

    def attempt(identity):
        current = Store(path)
        try:
            gate.wait()
            return current.open_intent(
                intent_id=identity, chat_id=1, topic_id=None,
                goal=identity, anchor_message_id=9,
            )
        finally:
            current.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("one", "two")))
    assert sorted(results) == [False, True]
    winner = ("one", "two")[results.index(True)]
    reopened = Store(path)
    try:
        assert [row["intent_id"] for row in reopened.open_intents(1, None)] == [winner]
    finally:
        reopened.close()


def test_foreign_or_missing_source_scope_is_rejected(store):
    source(store, chat_id=1, topic_id=7, message_id=31, sender_id=456)
    assert not store.open_intent(intent_id="foreign", chat_id=1, topic_id=8,
                                 goal="bad scope", anchor_message_id=31)
    assert not store.open_intent(intent_id="missing", chat_id=1, topic_id=7,
                                 goal="missing", anchor_message_id=32)
    assert store.open_intent(intent_id="valid", chat_id=1, topic_id=7,
                              goal="scoped", anchor_message_id=31)
    assert store.get_intent("valid", chat_id=1, topic_id=8) is None
    assert store.get_intent("foreign", chat_id=2, topic_id=7) is None
    assert [row["intent_id"] for row in store.open_intents(1, 7)] == ["valid"]


def test_owner_is_derived_and_restart_preserves_roots(tmp_path):
    path = tmp_path / "app.db"
    current = Store(path)
    source(current, message_id=41, sender_id=77)
    assert current.open_intent(intent_id="durable", chat_id=1, topic_id=None,
                               goal="persist", anchor_message_id=41, episode_id="ep")
    current.close()
    reopened = Store(path)
    try:
        rows = reopened.open_intents(1, None)
        assert len(rows) == 1
        assert (rows[0]["anchor_message_id"], rows[0]["owner_id"], rows[0]["episode_id"]) == (41, 77, "ep")
    finally:
        reopened.close()


def test_replacement_preserves_identity_and_cancels_only_linked_work(store):
    assert open_goal(store, "old", 1)
    assert open_goal(store, "other", 2)
    store.add_wait(wait_id="linked", chat_id=1, topic_id=None, kind="until",
                   purpose="linked timer", due_at=100, intent_id="old")
    store.add_wait(wait_id="independent", chat_id=1, topic_id=None, kind="until",
                   purpose="independent timer", due_at=100)
    store.prepare_action(action_id="old-pending", workflow_id=None, chat_id=1,
                         topic_id=None, text="stale reply", observed_through=0, intent_id="old")
    replacement = store.replace_intent(intent_id="new", chat_id=1, topic_id=None,
                                       goal="changed", replaces_intent_id="old")
    assert replacement["anchor_message_id"] == 1
    assert store.get_intent("old", chat_id=1, topic_id=None)["status"] == "abandoned"
    waits = {row["wait_id"]: row["status"] for row in store._query("SELECT * FROM agent_waits")}
    assert waits == {"linked": "cancelled", "independent": "active"}
    assert store.get_action("old-pending")["status"] == "cancelled"
    assert store.claim_action("old-pending") == "not-claimable"
    assert store.get_intent("new", chat_id=1, topic_id=None)["status"] == "open"
    assert store.get_intent("other", chat_id=1, topic_id=None)["status"] == "open"

    result = next(row for row in store.pending_observations(1, None)
                  if row["kind"] == "action_result")
    payload = json.loads(result["payload_json"])
    assert (payload["intent_id"], payload["anchor_message_id"]) == ("old", 1)

def test_replace_by_anchor_creates_then_replaces_only_that_root(store):
    source(store, message_id=90, sender_id=555)
    created = store.replace_intent(intent_id="created", chat_id=1, topic_id=None,
                                   goal="first", anchor_message_id=90)
    assert (created["intent_id"], created["owner_id"]) == ("created", 555)
    other = open_goal(store, "other", 91)
    changed = store.replace_intent(intent_id="changed", chat_id=1, topic_id=None,
                                   goal="revised", anchor_message_id=90)
    assert changed["anchor_message_id"] == 90
    assert store.get_intent("created", chat_id=1, topic_id=None)["status"] == "abandoned"
    assert store.get_intent("other", chat_id=1, topic_id=None)["status"] == "open"


def test_same_goal_replacement_is_idempotent_and_changed_goal_gets_new_id(store):
    assert open_goal(store, "original", 3, goal="same")
    same = store.replace_intent(intent_id="duplicate-id", chat_id=1, topic_id=None,
                                goal="same", replaces_intent_id="original")
    assert same["intent_id"] == "original"
    changed = store.replace_intent(intent_id="replacement", chat_id=1, topic_id=None,
                                   goal="different", replaces_intent_id="original")
    assert changed["intent_id"] == "replacement"
    assert changed["anchor_message_id"] == 3
    with pytest.raises(ValueError, match="unknown_intent"):
        store.replace_intent(intent_id="bad", chat_id=1, topic_id=None,
                             goal="x", replaces_intent_id="missing")


def test_completion_resolves_exact_action_identity_and_survives_replacement(store):
    assert open_goal(store, "old", 4)
    store.prepare_action(action_id="answer-old", workflow_id=None, chat_id=1,
                         topic_id=None, text="answer", observed_through=0,
                         intent_id="old", resolves_intent=True)
    assert store.claim_action("answer-old") == "claimed"
    replacement = store.replace_intent(intent_id="new", chat_id=1, topic_id=None,
                                       goal="new goal", replaces_intent_id="old")
    # A send already claimed can complete, but its identity cannot close the replacement.
    assert store.finish_action("answer-old", "sent", telegram_message_id=51)
    assert store.get_intent("new", chat_id=1, topic_id=None)["status"] == "open"
    assert replacement["intent_id"] == "new"


@pytest.mark.parametrize("outcome", ["failed", "unknown"])
def test_non_sent_delivery_keeps_commitment_open_and_is_terminal(store, outcome):
    assert open_goal(store, "goal", 40)
    store.prepare_action(action_id="uncertain", workflow_id=None, chat_id=1,
                         topic_id=None, text="reply", observed_through=0,
                         intent_id="goal", resolves_intent=True)
    assert store.claim_action("uncertain") == "claimed"
    assert store.finish_action("uncertain", outcome, error="delivery outcome")
    assert store.get_intent("goal", chat_id=1, topic_id=None)["status"] == "open"
    assert store.claim_action("uncertain") == "not-claimable"
    assert store.get_action("uncertain")["status"] == outcome


def test_delivery_closes_exact_goal_and_its_wait_atomically(store):
    assert open_goal(store, "goal", 5)
    store.add_wait(wait_id="goal-wait", chat_id=1, topic_id=None, kind="until",
                   purpose="deadline", due_at=10, intent_id="goal")
    store.add_wait(wait_id="other-wait", chat_id=1, topic_id=None, kind="until",
                   purpose="independent", due_at=10)
    store.prepare_action(action_id="complete", workflow_id=None, chat_id=1,
                         topic_id=None, text="done", observed_through=0,
                         intent_id="goal", resolves_intent=True)
    assert store.claim_action("complete") == "claimed"
    assert store.finish_action("complete", "sent", telegram_message_id=80)
    assert store.get_intent("goal", chat_id=1, topic_id=None)["status"] == "resolved"
    waits = {row["wait_id"]: row["status"] for row in store._query("SELECT * FROM agent_waits")}
    assert waits == {"goal-wait": "cancelled", "other-wait": "active"}


def test_linked_timer_ignores_unrelated_messages_and_exposes_identity(store):
    assert open_goal(store, "timer-goal", 6)
    store.add_wait(wait_id="timer", chat_id=1, topic_id=None, kind="until",
                   purpose="wait until deadline", due_at=20, intent_id="timer-goal")
    store.add_wait(wait_id="event", chat_id=1, topic_id=None, kind="event",
                   purpose="wait for message", intent_id="timer-goal")
    assert store.satisfy_event_waits(1, None, "message", 10) == ["event"]
    assert store.active_waits(1, None)[0]["anchor_message_id"] == 6
    assert store.matching_waits_for_observations(1, None, [(77, "message", 200)], 10) == []
    due = store.due_waits(20)
    timer = next(row for row in due if row["wait_id"] == "timer")
    assert (timer["intent_id"], timer["anchor_message_id"]) == ("timer-goal", 6)


def test_expiry_cancels_only_linked_waits_and_reports_identity(store):
    assert open_goal(store, "expires", 7, expires_at=1)
    store.add_wait(wait_id="linked", chat_id=1, topic_id=None, kind="until",
                   purpose="linked", due_at=20, intent_id="expires")
    store.add_wait(wait_id="independent", chat_id=1, topic_id=None, kind="until",
                   purpose="independent", due_at=20)
    store.prepare_action(action_id="expired-action", workflow_id=None, chat_id=1,
                         topic_id=None, text="stale", observed_through=0, intent_id="expires")
    store.prepare_action(action_id="independent-action", workflow_id=None, chat_id=1,
                         topic_id=None, text="independent", observed_through=0)
    expired = store.expire_intents(now=2)
    assert [row["intent_id"] for row in expired] == ["expires"]
    event = next(row for row in store.pending_observations(1, None)
                 if row["kind"] == "intent_expired")
    assert json.loads(event["payload_json"])["anchor_message_id"] == 7
    assert store.get_action("expired-action")["status"] == "cancelled"
    assert store.get_action("independent-action")["status"] == "pending"
    assert {row["wait_id"]: row["status"] for row in store._query("SELECT * FROM agent_waits")} == {
        "linked": "cancelled", "independent": "active"}


def test_capacity_32_and_replacement_at_capacity(store):
    for anchor in range(1, 33):
        assert open_goal(store, f"i{anchor}", anchor)
    assert len(store.open_intents(1, None)) == 32
    assert open_goal(store, "over-capacity", 33) is False
    replacement = store.replace_intent(intent_id="replacement", chat_id=1, topic_id=None,
                                       goal="replacement", replaces_intent_id="i1")
    assert replacement["intent_id"] == "replacement"
    assert len(store.open_intents(1, None)) == 32


def test_legacy_schema_migration_preserves_unanchored_goal_and_links(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE messages (
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, topic_id INTEGER,
            sender_id INTEGER, sender_label TEXT NOT NULL, is_bot INTEGER NOT NULL,
            text TEXT NOT NULL, created_at REAL NOT NULL, update_id INTEGER NOT NULL,
            reply_to_bot INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat_id, message_id));
        CREATE TABLE workflow_episodes (
            episode_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, topic_id INTEGER,
            state TEXT NOT NULL, reason TEXT, intent_id TEXT, opened_at REAL NOT NULL,
            updated_at REAL NOT NULL, closed_at REAL);
        CREATE TABLE agent_intents (
            intent_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, topic_id INTEGER,
            goal TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
            updated_at REAL NOT NULL, expires_at REAL, resolved_by TEXT, resolved_at REAL);
        CREATE UNIQUE INDEX agent_intents_one_open ON agent_intents(chat_id, topic_id) WHERE status='open';
        CREATE UNIQUE INDEX agent_intents_one_open_no_topic ON agent_intents(chat_id) WHERE status='open' AND topic_id IS NULL;
        CREATE TABLE agent_waits (
            wait_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, topic_id INTEGER,
            kind TEXT NOT NULL, purpose TEXT NOT NULL, event_types_json TEXT,
            due_at REAL, expires_at REAL, status TEXT NOT NULL, created_at REAL NOT NULL,
            resolved_at REAL, episode_id TEXT);
        CREATE TABLE actions (
            action_id TEXT PRIMARY KEY, workflow_id TEXT, chat_id INTEGER NOT NULL,
            topic_id INTEGER, reply_to INTEGER, text TEXT NOT NULL,
            observed_through INTEGER NOT NULL, freshness TEXT NOT NULL DEFAULT 'strict',
            max_unobserved INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, telegram_message_id INTEGER, error TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL,
            intent_id TEXT);
        INSERT INTO workflow_episodes VALUES ('ep', 1, NULL, 'open', NULL, 'legacy', 1, 1, NULL);
        INSERT INTO agent_intents VALUES ('legacy', 1, NULL, 'old goal', 'open', 1, 1, NULL, NULL, NULL);
        INSERT INTO agent_waits VALUES ('wait', 1, NULL, 'until', 'old timer', NULL, 99, NULL, 'active', 1, NULL, 'ep');
        INSERT INTO actions VALUES ('action', NULL, 1, NULL, NULL, 'old reply', 0, 'strict', 0,
                                   'pending', 0, NULL, NULL, 1, 1, NULL, 'legacy');
        INSERT INTO actions VALUES ('sent-action', NULL, 1, NULL, NULL, 'sent reply', 0, 'strict', 0,
                                   'sent', 1, 99, NULL, 1, 1, NULL, 'legacy');
        INSERT INTO actions VALUES ('unknown-action', NULL, 1, NULL, NULL, 'unknown reply', 0, 'strict', 0,
                                   'unknown', 1, NULL, 'unconfirmed', 1, 1, NULL, 'legacy');
    """)
    conn.commit()
    conn.close()

    migrated = Store(path)
    try:
        intents = migrated.open_intents(1, None)
        assert len(intents) == 1
        assert (intents[0]["intent_id"], intents[0]["anchor_message_id"],
                intents[0]["owner_id"], intents[0]["episode_id"]) == ("legacy", None, None, "ep")
        wait = migrated.active_waits(1, None)[0]
        assert wait["intent_id"] == "legacy"
        action = migrated.get_action("action")
        assert (action["intent_id"], action["resolves_intent"]) == ("legacy", 1)
        assert migrated.get_action("sent-action")["status"] == "sent"
        assert migrated.get_action("unknown-action")["status"] == "unknown"
        assert "intent_id" not in {row["name"] for row in
                                   migrated._conn.execute("PRAGMA table_info(workflow_episodes)")}
        assert migrated._query("SELECT value FROM meta WHERE key='schema_version'")[0][0] == str(SCHEMA_VERSION)
        reopened = Store(path)
        try:
            assert reopened.open_intents(1, None)[0]["intent_id"] == "legacy"
            assert reopened.active_waits(1, None)[0]["intent_id"] == "legacy"
            assert reopened.get_action("action")["resolves_intent"] == 1
        finally:
            reopened.close()
    finally:
        migrated.close()


def test_inbound_observation_exposes_reply_target(store):

    status = store.record_inbound_message(
        StoredMessage(1, 100, None, 55, "alice", False, "reply", 1),
        update_id=200, payload={"reply_to_message_id": 81},
    )
    assert status == "message"
    payload = json.loads(store.pending_observations(1, None)[0]["payload_json"])
    assert payload["reply_to_message_id"] == 81
