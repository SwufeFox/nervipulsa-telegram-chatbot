"""Concurrency and freshness regressions for the durable action outbox."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import sqlite3

import pytest

from app.store import Store


def prepare(store, action_id, *, topic=1, watermark=0, freshness="strict", limit=0):
    assert store.prepare_action(
        action_id=action_id, workflow_id=None, chat_id=1, topic_id=topic,
        text="hello", observed_through=watermark, freshness=freshness,
        max_unobserved=limit,
    )


def insert(conn, update_id, topic=1, chat=1):
    conn.execute(
        "INSERT INTO observation_events (update_id, chat_id, topic_id, kind, received_at, payload_json)"
        " VALUES (?, ?, ?, 'message', 0, '{}')", (update_id, chat, topic),
    )


def test_two_connections_only_one_claims(tmp_path):
    path = tmp_path / "actions.db"
    store = Store(path)
    prepare(store, "a")
    store.close()
    barrier = Barrier(2)

    def claim():
        worker = Store(path)
        try:
            barrier.wait(timeout=5)
            return worker.claim_action("a")
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sorted(results) == ["claimed", "not-claimable"]
    store = Store(path)
    try:
        assert store.get_action("a")["attempts"] == 1
    finally:
        store.close()


def test_input_committed_before_claim_is_stale(tmp_path):
    path = tmp_path / "actions.db"
    store = Store(path)
    prepare(store, "a")
    store.close()
    ready, start = Event(), Event()

    def claim():
        worker = Store(path)
        try:
            ready.set()
            assert start.wait(5)
            return worker.claim_action("a")
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(claim)
        assert ready.wait(5)
        with sqlite3.connect(path) as writer:
            writer.execute("BEGIN IMMEDIATE")
            insert(writer, 1)
            start.set()
            assert not future.done()
        assert future.result(timeout=5) == "stale"


def test_claim_holds_write_lock_through_freshness_check(tmp_path):
    path = tmp_path / "actions.db"
    store = Store(path)
    prepare(store, "a")
    store.close()
    checked, release = Event(), Event()

    def claim():
        worker = Store(path)
        def trace(sql):
            if sql.startswith("SELECT COUNT(*) FROM observation_events"):
                checked.set()
                if not release.wait(5):
                    raise RuntimeError("claim was not released")
        worker._conn.set_trace_callback(trace)
        try:
            return worker.claim_action("a")
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(claim)
        try:
            assert checked.wait(5)
            with sqlite3.connect(path, timeout=0) as writer:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    insert(writer, 1)
        finally:
            release.set()
        assert future.result(timeout=5) == "claimed"
    with sqlite3.connect(path) as writer:
        insert(writer, 1)
    store = Store(path)
    try:
        assert store.get_action("a")["status"] == "sending"
    finally:
        store.close()


@pytest.mark.parametrize("topic", [None, 1])
def test_bounded_counts_events_not_global_sequence_gaps(tmp_path, topic):
    store = Store(tmp_path / "actions.db")
    try:
        with store._conn:
            insert(store._conn, 1, topic)
            for uid in range(2, 12):
                insert(store._conn, uid, 2)
            insert(store._conn, 12, topic)
            insert(store._conn, 13, topic, chat=2)
        prepare(store, "within", topic=topic, watermark=1, freshness="bounded", limit=1)
        prepare(store, "strict", topic=topic, watermark=1)
        prepare(store, "zero", topic=topic, watermark=1, freshness="bounded", limit=0)
        assert store.claim_action("within") == "claimed"
        assert store.claim_action("strict") == "stale"
        assert store.claim_action("zero") == "stale"
        with store._conn:
            insert(store._conn, 14, topic)
        prepare(store, "over", topic=topic, watermark=1, freshness="bounded", limit=1)
        assert store.claim_action("over") == "stale"
    finally:
        store.close()
