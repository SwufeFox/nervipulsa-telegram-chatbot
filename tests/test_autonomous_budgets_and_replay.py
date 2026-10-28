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
            assert second[0].outcome == "deferred_budget"
            assert len(calls) == 1, "the model ran despite an exhausted budget"
            # Not silent: the observation is still queued for later.
            assert store.pending_observations(
                fixtures.TEST_CHAT, fixtures.TOPIC_A), "deferred turn dropped its input"
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_speak_interval_defers_without_dropping_observations(tmp_path):
    async def scenario():
        bot, store, clock, transport, handle = build(
            tmp_path, lambda request: ModelResponse(
                content="", model="scripted",
                tool_calls=[ToolCall(id="c", name="send_message",
                                     arguments={"text": "hello"})]),
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
            assert second and second[0].state == "deferred", second
            assert second[0].outcome == "deferred_budget"
            assert [s["text"] for s in transport.sends] == ["hello"]
            assert store.pending_observations(
                fixtures.TEST_CHAT, fixtures.TOPIC_A), "input was dropped"

            # Once the interval passes the queued observation is served.
            clock.advance(400)
            third = await bot.autonomous_step(force=True)
            assert third and third[0].state == "completed", third
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
