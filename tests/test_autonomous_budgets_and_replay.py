"""Regressions for two defects found after the first autonomous release.

1. Restart counted unprocessed observations but never re-armed them, so a
   message received just before a crash sat past the cursor forever.
2. Nothing bounded model cost or speak frequency once ParticipationPolicy was
   removed. The budget must defer a turn, never record it as the agent having
   decided to stay silent.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nervipulsa.config import Settings
from nervipulsa.ports import ModelResponse, ToolCall
from nervipulsa.providers import ScriptedBackend

from app import fixtures
from app.agent import NervipulsaAgent
from app.bot import GroupAgent
from app.config import AppConfig, load_default_profile
from app.store import Store
from app.telegram import TelegramClient
from app.turns import TurnManager
from conftest import FakeClock, FakeTransport, run


def msg(uid, text, topic=None):
    return fixtures.message(update_id=uid, message_id=uid, sender_id=fixtures.ALICE,
                            username="alice", topic_id=topic or fixtures.TOPIC_A,
                            text=text)


def silent(request):
    return ModelResponse(content="", model="scripted",
                         tool_calls=[ToolCall(id="c", name="finish_silently",
                                              arguments={})])


def build(tmp_path, responder=silent, *, db=None, **overrides):
    clock = FakeClock()
    store = Store(db or (tmp_path / "app.db"), clock=clock)
    transport = FakeTransport()
    prompt = load_default_profile()
    settings = Settings(model="scripted", workspace=str(tmp_path / "rt"),
                        max_timeout=30, default_timeout=30, context_limit=40000)
    handle = {}

    def factory(spec):
        handle["agent"] = NervipulsaAgent(
            settings=settings, workspace=spec.workspace, system_prompt=prompt,
            backend=ScriptedBackend(responder), timeout=10,
            tool_catalog=spec.tool_catalog,
            response_commit_hook=spec.response_commit_hook,
        )
        return handle["agent"]

    kwargs = dict(
        bot_name="Alma", bot_username="alma_bot",
        allowed_chat_ids=(fixtures.TEST_CHAT,), idle_seconds=0.0,
        workspace=str(tmp_path), model="scripted",
    )
    kwargs.update(overrides)
    config = AppConfig(**kwargs)
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=prompt, agent_factory=factory,
                     turn_manager=TurnManager(debounce_seconds=2, clock=clock),
                     bot_user_id=fixtures.ALMA, clock=clock)
    return bot, store, clock, transport, handle


def test_restart_replays_observations_left_past_the_cursor(tmp_path):
    """A crash before any activation must not strand the message."""
    db = tmp_path / "app.db"

    async def crash():
        bot, store, clock, _, _ = build(tmp_path, db=db)
        await bot.start()
        await bot.step([msg(701, "received just before the crash")])
        # No activation happens: the process is assumed to die here.
        await bot.stop()
        store.close()

    run(crash())

    async def restart():
        calls = []

        def responder(request):
            calls.append(1)
            return silent(request)

        bot, store, clock, transport, handle = build(tmp_path, responder, db=db)
        await bot.start()
        try:
            assert bot.recovered_inbound == 1
            assert bot.queued_topics(), "restart did not re-arm the pending scope"

            clock.advance(10)
            results = await bot.autonomous_step(force=False)

            assert calls, "restarted process never asked the model"
            assert results and results[0].state == "completed"
            assert store.pending_observations(fixtures.TEST_CHAT, fixtures.TOPIC_A) == []
        finally:
            await bot.stop()
            store.close()

    run(restart())


def test_restart_does_not_replay_already_processed_observations(tmp_path):
    """Replay must respect the cursor, or every restart would repeat the chat."""
    db = tmp_path / "app.db"

    async def first_run():
        bot, store, clock, _, _ = build(tmp_path, db=db)
        await bot.start()
        await bot.step([msg(701, "already handled")])
        clock.advance(5)
        await bot.autonomous_step()
        await bot.stop()
        store.close()

    run(first_run())

    async def restart():
        calls = []
        bot, store, clock, _, _ = build(
            tmp_path, lambda request: calls.append(1) or silent(request), db=db)
        await bot.start()
        try:
            assert bot.recovered_inbound == 0
            clock.advance(10)
            results = await bot.autonomous_step(force=False)
            assert results == []
            assert calls == []
        finally:
            await bot.stop()
            store.close()

    run(restart())


def test_hourly_activation_budget_defers_instead_of_silencing(tmp_path):
    async def scenario():
        calls = []

        def responder(request):
            calls.append(1)
            return silent(request)

        # One activation per hour, and the scope already used it.
        bot, store, clock, transport, handle = build(
            tmp_path, responder, max_activations_per_hour=1)
        await bot.start()
        await bot.step([msg(701, "first")])
        clock.advance(5)
        first = await bot.autonomous_step()
        assert first and first[0].state == "completed"
        assert len(calls) == 1

        await bot.step([msg(702, "second")])
        clock.advance(5)
        second = await bot.autonomous_step()
        try:
            assert second and second[0].state == "deferred", second
            # The reason names which limit bound, so a stuck budget is
            # diagnosable from the log instead of just "deferred".
            assert second[0].outcome.startswith("activations_per_hour"), second
            assert len(calls) == 1, "the model ran despite an exhausted budget"
            # Not silent: the observation is still queued for later.
            assert store.pending_observations(
                fixtures.TEST_CHAT, fixtures.TOPIC_A), "deferred turn dropped its input"
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_speak_interval_defers_send_without_blocking_decision(tmp_path):
    async def scenario():
        bot, store, clock, transport, handle = build(
            tmp_path, lambda request: ModelResponse(
                content="", model="scripted",
                tool_calls=[ToolCall(id="c", name="send_message",
                                     arguments={"text": "hello",
                                                "freshness": "bounded",
                                                "max_unobserved_events": 100})]),
            min_action_interval_seconds=300.0, max_actions_per_hour=100)
        await bot.start()
        await bot.step([msg(701, "say hi")])
        clock.advance(5)
        first = await bot.autonomous_step()
        try:
            assert first and first[0].state == "completed"
            assert [s["text"] for s in transport.sends] == ["hello"]

            await bot.step([msg(702, "say hi again")])
            clock.advance(5)
            second = await bot.autonomous_step()
            assert second and second[0].state == "completed", second
            assert [s["text"] for s in transport.sends] == ["hello"]
            assert not store.pending_observations(fixtures.TEST_CHAT, fixtures.TOPIC_A)
            assert len(store.pending_actions()) == 1

            # The durable intent sends once after the interval, without a new model call.
            clock.advance(400)
            assert await bot.autonomous_step() == []
            assert [s["text"] for s in transport.sends] == ["hello", "hello"]
            assert not store.pending_actions()
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_hourly_send_limit_preserves_decisions_and_delivery_time(tmp_path):
    async def scenario():
        calls = []

        def responder(request):
            calls.append(1)
            return ModelResponse(content="", model="scripted", tool_calls=[
                ToolCall(id="c", name="send_message",
                         arguments={"text": str(len(calls)),
                                    "freshness": "bounded",
                                    "max_unobserved_events": 100})])

        bot, store, clock, transport, _ = build(
            tmp_path, responder, max_actions_per_hour=1, min_action_interval_seconds=0)
        await bot.start()
        try:
            await bot.step([msg(801, "one")])
            await bot.autonomous_step(force=True)
            await bot.step([msg(802, "two")])
            await bot.autonomous_step(force=True)
            assert len(calls) == 2
            assert [s["text"] for s in transport.sends] == ["1"]
            assert len(store.pending_actions()) == 1
            for _ in range(5):
                assert await bot.autonomous_step() == []
            assert len(calls) == 2
            clock.advance(3601)
            await bot.autonomous_step()
            assert [s["text"] for s in transport.sends] == ["1", "2"]
            await bot.step([msg(803, "three")])
            await bot.autonomous_step(force=True)
            assert len(calls) == 3
            assert [s["text"] for s in transport.sends] == ["1", "2"]
            assert len(store.pending_actions()) == 1
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_pending_strict_action_is_refreshed_after_new_input(tmp_path):
    async def scenario():
        bot, store, clock, transport, _ = build(
            tmp_path, lambda request: ModelResponse(content="", model="scripted",
                tool_calls=[ToolCall(id="c", name="send_message",
                                     arguments={"text": "hello", "freshness": "strict"})]),
            min_action_interval_seconds=300, max_actions_per_hour=100)
        await bot.start()
        try:
            await bot.step([msg(811, "one")])
            await bot.autonomous_step(force=True)
            await bot.step([msg(812, "two")])
            await bot.autonomous_step(force=True)
            old_id = store.pending_actions()[0]["action_id"]
            await bot.step([msg(813, "new information")])
            await bot.autonomous_step(force=True)
            clock.advance(301)
            await bot.autonomous_step()
            assert store.get_action(old_id)["status"] == "cancelled"
            assert len(transport.sends) == 2
            assert not store.pending_observations(fixtures.TEST_CHAT, fixtures.TOPIC_A)
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_throttled_backlog_does_not_starve_another_chat(tmp_path):
    async def scenario():
        bot, store, clock, transport, _ = build(
            tmp_path, min_action_interval_seconds=300, max_actions_per_hour=100)
        await bot.start()
        try:
            store.prepare_action(action_id="sent", workflow_id=None, chat_id=1,
                                 topic_id=None, text="first", observed_through=0)
            await bot.autonomy.flush_actions()
            for i in range(55):
                store.prepare_action(action_id=f"blocked{i}", workflow_id=None, chat_id=1,
                                     topic_id=None, text="blocked", observed_through=0)
            store.prepare_action(action_id="other", workflow_id=None, chat_id=2,
                                 topic_id=None, text="other", observed_through=0)
            await bot.autonomy.flush_actions()
            assert [s["text"] for s in transport.sends] == ["first", "other"]
            assert len(store.pending_actions(limit=100)) == 55
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_unknown_send_consumes_budget_and_is_never_retried(tmp_path):
    async def scenario():
        from types import SimpleNamespace

        bot, store, clock, _, _ = build(
            tmp_path, min_action_interval_seconds=300, max_actions_per_hour=100)
        attempts = []

        async def unknown(**kwargs):
            attempts.append(kwargs["text"])
            return SimpleNamespace(status="unknown", error="timeout")

        await bot.start()
        bot.autonomy._send = unknown
        try:
            for action_id in ("a", "b"):
                store.prepare_action(action_id=action_id, workflow_id=None, chat_id=1,
                                     topic_id=None, text=action_id, observed_through=0,
                                     freshness="bounded", max_unobserved=10)
            await bot.autonomy.flush_actions()
            assert attempts == ["a"]
            assert store.get_action("a")["status"] == "unknown"
            assert store.get_action("b")["status"] == "pending"
            clock.advance(301)
            await bot.autonomy.flush_actions()
            await bot.autonomy.flush_actions()
            assert attempts == ["a", "b"]
            assert store.get_action("a")["attempts"] == 1
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_budget_counts_unknown_delivery_as_spent(tmp_path):
    """An unknown send may be visible, so it must consume the hourly budget."""
    store = Store(tmp_path / "a.db")
    try:
        store.prepare_action(action_id="a1", workflow_id=None,
                             chat_id=fixtures.TEST_CHAT, topic_id=None,
                             text="maybe visible", observed_through=0)
        assert store.claim_action("a1") == "claimed"
        store.finish_action("a1", "unknown", error="timeout")
        assert store.count_actions_since(fixtures.TEST_CHAT, 0.0) == 1
    finally:
        store.close()


def test_deferred_scope_is_not_retried_on_every_tick(tmp_path):
    """A deferred scope must wait, or the scheduler spins on a closed window."""
    async def scenario():
        calls = []

        def responder(request):
            calls.append(1)
            return ModelResponse(content="", model="scripted",
                                 tool_calls=[ToolCall(id="c", name="send_message",
                                                      arguments={"text": "x"})])

        bot, store, clock, transport, handle = build(
            tmp_path, responder, max_actions_per_hour=1,
            min_action_interval_seconds=600.0)
        await bot.start()
        await bot.step([msg(701, "one")])
        clock.advance(5)
        await bot.autonomous_step()

        await bot.step([msg(702, "two")])
        clock.advance(5)
        await bot.autonomous_step()
        before = bot.autonomy.deferred
        # Many ticks inside the closed window must not each re-defer.
        for _ in range(5):
            await bot.autonomous_step(force=False)
        try:
            assert bot.autonomy.deferred == before, "deferred scope retried too eagerly"
        finally:
            await bot.stop()
            store.close()

    run(scenario())


# -- cursor identity -------------------------------------------------------
# A non-forum chat has topic_id NULL, and SQLite treats NULL as distinct from
# itself inside a UNIQUE/PRIMARY KEY. The cursor table therefore accepted a new
# row per completed workflow, the read path returned whichever row came first,
# and the watermark sat at its oldest value forever. In production that showed
# up as seven cursor rows for one private chat.


def test_cursor_collapses_across_many_turns_in_one_scope(tmp_path):
    store = Store(tmp_path / "cursor.db")
    try:
        for i in range(1, 6):
            store._conn.execute(
                "INSERT INTO observation_events"
                " (update_id, chat_id, topic_id, kind, received_at, payload_json)"
                " VALUES (?, ?, NULL, 'message', ?, ?)",
                (i, fixtures.TEST_CHAT, 100.0 + i, '{"text": "x"}'),
            )
            store._conn.commit()
            workflow = store.open_workflow(
                chat_id=fixtures.TEST_CHAT, topic_id=None,
                first_sequence=i, last_sequence=i, workflow_id=f"wf{i}",
            )
            assert store.close_workflow(workflow, "completed", outcome="silent")

        rows = store._query("SELECT * FROM observation_cursors")
        assert len(rows) == 1, [dict(row) for row in rows]
        assert store.observation_cursor(fixtures.TEST_CHAT, None) == 5
        assert store.pending_observations(fixtures.TEST_CHAT, None) == []
    finally:
        store.close()


def test_topic_and_non_topic_scopes_stay_separate(tmp_path):
    store = Store(tmp_path / "scopes.db")
    try:
        for topic, seq in ((None, 1), (fixtures.TOPIC_A, 2), (fixtures.TOPIC_B, 3)):
            store._conn.execute(
                "INSERT INTO observation_events"
                " (update_id, chat_id, topic_id, kind, received_at, payload_json)"
                " VALUES (?, ?, ?, 'message', ?, ?)",
                (seq, fixtures.TEST_CHAT, topic, 100.0 + seq, '{"text": "x"}'),
            )
            store._conn.commit()
            workflow = store.open_workflow(
                chat_id=fixtures.TEST_CHAT, topic_id=topic,
                first_sequence=seq, last_sequence=seq, workflow_id=f"wf{seq}",
            )
            store.close_workflow(workflow, "completed", outcome="silent")

        assert store.observation_cursor(fixtures.TEST_CHAT, None) == 1
        assert store.observation_cursor(fixtures.TEST_CHAT, fixtures.TOPIC_A) == 2
        assert store.observation_cursor(fixtures.TEST_CHAT, fixtures.TOPIC_B) == 3
    finally:
        store.close()


def test_broken_cursor_table_is_migrated_in_place(tmp_path):
    """A database written before the fix must heal, keeping the newest value."""
    path = tmp_path / "legacy.db"
    store = Store(path)
    store._conn.execute("DROP TABLE observation_cursors")
    store._conn.executescript(
        "CREATE TABLE observation_cursors ("
        " chat_id INTEGER NOT NULL, topic_id INTEGER,"
        " last_processed INTEGER NOT NULL, updated_at REAL NOT NULL,"
        " PRIMARY KEY (chat_id, topic_id));"
    )
    for value in (2, 3, 3, 4, 5):
        store._conn.execute(
            "INSERT INTO observation_cursors VALUES (?, NULL, ?, ?)",
            (fixtures.TEST_CHAT, value, 100.0 + value),
        )
    store._conn.commit()
    store.close()

    reopened = Store(path)
    try:
        rows = reopened._query("SELECT * FROM observation_cursors")
        assert len(rows) == 1, [dict(row) for row in rows]
        # The greatest watermark wins: never roll progress backwards.
        assert reopened.observation_cursor(fixtures.TEST_CHAT, None) == 5
    finally:
        reopened.close()
