"""Atomic action outcomes and restart recovery."""
import json
import sqlite3

import pytest

from app.store import Store


def prepare(store, action_id):
    store.prepare_action(action_id=action_id, workflow_id=None, chat_id=1,
                         topic_id=None, text="hello", observed_through=0,
                         freshness="bounded", max_unobserved=100)
    assert store.claim_action(action_id) == "claimed"


def test_terminal_event_rolls_back_and_recovery_is_idempotent(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        prepare(store, "a")
        store._conn.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON observation_events BEGIN SELECT RAISE(ABORT, 'receipt failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            store.finish_action("a", "unknown", error="timeout")
        assert store.get_action("a")["status"] == "sending"
        store._conn.execute("DROP TRIGGER reject_receipt")
        assert store.recover_orphaned_actions() == ["a"]
        assert store.recover_orphaned_actions() == []
        rows = store.pending_observations(1, None)
        assert len(rows) == 1
        assert json.loads(rows[0]["payload_json"])["status"] == "unknown"
        assert not store.finish_action("a", "failed", error="late result")
        assert store.get_action("a")["status"] == "unknown"
    finally:
        store.close()


def test_unfinished_workflow_cannot_send_and_failure_cancels_intent(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        store.open_workflow(chat_id=1, topic_id=None, first_sequence=1,
                            last_sequence=1, workflow_id="wf")
        store.prepare_action(action_id="a", workflow_id="wf", chat_id=1,
                             topic_id=None, text="hello", observed_through=0)
        assert store.claim_action("a") == "not-claimable"
        assert store.close_workflow("wf", "failed", outcome="interrupted_by_restart")
        assert store.get_action("a")["status"] == "cancelled"
        assert store.observation_cursor(1, None) == 0
        assert store.claim_action("a") == "not-claimable"
    finally:
        store.close()


def test_success_history_is_atomic_and_does_not_wake(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        prepare(store, "a")
        store._conn.execute("CREATE TRIGGER reject_message BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT, 'history failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            store.finish_action("a", "sent", telegram_message_id=42)
        assert store.get_action("a")["status"] == "sending"
        store._conn.execute("DROP TRIGGER reject_message")
        assert store.finish_action("a", "sent", telegram_message_id=42)
        assert len(store._query("SELECT * FROM messages WHERE message_id = 42")) == 1
        assert store.pending_observations(1, None) == []
    finally:
        store.close()


def test_recovery_adopts_legacy_event_and_repairs_missing_event(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        prepare(store, "old")
        with store._conn:
            store._conn.execute("UPDATE actions SET status = 'unknown' WHERE action_id = 'old'")
            store._conn.execute("INSERT INTO observation_events (update_id, chat_id, topic_id, kind, received_at, payload_json) VALUES (-1, 1, NULL, 'action_result', 0, ?)",
                                (json.dumps({"action_id": "old", "status": "unknown"}),))
        prepare(store, "missing")
        with store._conn:
            store._conn.execute("UPDATE actions SET status = 'failed' WHERE action_id = 'missing'")
        store.recover_orphaned_actions()
        store.recover_orphaned_actions()
        events = store.pending_observations(1, None)
        assert {json.loads(row["payload_json"])["action_id"] for row in events} == {"old", "missing"}
        assert len(events) == 2
    finally:
        store.close()
