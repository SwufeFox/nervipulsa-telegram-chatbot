"""A timer the agent sets must actually wake it, exactly once.

Found by reading the code, not by a test: `_wake_due` only called
`note_activity()`, and `_batches()` discards a window with no pending
observations. So `wait_until` never produced an activation by itself - it
worked only when somebody unrelated happened to speak. Worse, the wait row was
never resolved, so `due_waits` returned it on every tick: the scheduler woke,
found nothing to do, and woke again.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

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


def build(tmp_path, responder):
    clock = FakeClock()
    store = Store(tmp_path / "app.db", clock=clock)
    transport = FakeTransport()
    prompt = load_default_profile()
    settings = Settings(model="scripted", workspace=str(tmp_path / "rt"),
                        max_timeout=30, default_timeout=30, context_limit=40000)
    seen = []

    def factory(spec):
        def record(request):
            seen.append(request)
            return responder(request)

        return NervipulsaAgent(
            settings=settings, workspace=spec.workspace, system_prompt=prompt,
            backend=ScriptedBackend(record), timeout=10,
            tool_catalog=spec.tool_catalog,
            response_commit_hook=spec.response_commit_hook,
            session_id=spec.session_id, restore=spec.restore,
        )

    config = AppConfig(
        bot_name="Alma", bot_username="alma_bot",
        allowed_chat_ids=(fixtures.TEST_CHAT,), idle_seconds=0.0,
        workspace=str(tmp_path), model="scripted",
        min_action_interval_seconds=0.0, max_actions_per_hour=99,
    )
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=prompt, agent_factory=factory,
                     turn_manager=TurnManager(debounce_seconds=2, clock=clock),
                     bot_user_id=fixtures.ALMA, clock=clock)
    return bot, store, clock, transport, seen


def test_due_timer_wakes_the_agent_with_no_new_messages(tmp_path):
    """The whole point of wait_until: come back later, on your own."""
    calls = []

    def responder(request):
        calls.append(1)
        if len(requests_seen := calls) == 1:
            # First turn: register a timer rather than answering now.
            return ModelResponse(content="", model="scripted", tool_calls=[
                ToolCall(id="c", name="wait_until",
                         arguments={"at": "2030-01-01T00:00:00+00:00",
                                    "purpose": "等她回话"})])
        return ModelResponse(content="", model="scripted", tool_calls=[
            ToolCall(id="c", name="send_message", arguments={"text": "还等着呢"})])

    async def scenario():
        bot, store, clock, transport, seen = build(tmp_path, responder)
        await bot.start()
        try:
            # Put the wait in the past so it is due without waiting a year.
            await bot.step([msg(701, "我先去忙了")])
            clock.advance(5)
            first = await bot.autonomous_step(force=True)
            assert first and first[0].state == "completed", first
            assert transport.sends == [], "wait_until must not send anything"

            wait = store._query("SELECT * FROM agent_waits")[0]
            assert wait["status"] == "active"

            # No new Telegram message at all. Only time passes.
            clock.advance(3600 * 24 * 365 * 10)
            await bot.autonomous_step(force=False)

            assert len(calls) == 2, "the due timer never woke the agent"
            assert [s["text"] for s in transport.sends] == ["还等着呢"]

            waits = store._query("SELECT status FROM agent_waits")
            assert [w["status"] for w in waits] == ["satisfied"], (
                "a fired timer must be resolved or it fires on every tick"
            )
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_timer_payload_carries_the_purpose_it_was_set_for(tmp_path):
    """Being woken with no explanation is indistinguishable from an interruption."""
    def responder(request):
        return ModelResponse(content="", model="scripted", tool_calls=[
            ToolCall(id="c", name="wait_until",
                     arguments={"at": "2030-01-01T00:00:00+00:00",
                                "purpose": "看她有没有把日志贴出来"})])

    async def scenario():
        bot, store, clock, transport, seen = build(tmp_path, responder)
        await bot.start()
        try:
            await bot.step([msg(711, "我去找日志")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            clock.advance(3600 * 24 * 365 * 10)
            # Record what the second activation was given.
            second_bodies = []
            original = responder

            await bot.autonomous_step(force=False)
            body = json.dumps(seen[-1].messages, ensure_ascii=False)
            assert "timer" in body
            assert "看她有没有把日志贴出来" in body, (
                "the wake did not carry the reason the agent set the timer"
            )
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_a_due_timer_fires_once_not_on_every_tick(tmp_path):
    """An unresolved wait made due_waits return it forever: a spin."""
    fired = []

    def responder(request):
        if fired:
            # One wait only: a second wait would be a second, legitimate timer.
            return ModelResponse(content="", model="scripted", tool_calls=[
                ToolCall(id="c", name="finish_silently", arguments={})])
        fired.append(1)
        return ModelResponse(content="", model="scripted", tool_calls=[
            ToolCall(id="c", name="wait_until",
                     arguments={"at": "2030-01-01T00:00:00+00:00",
                                "purpose": "等两分钟"})])

    async def scenario():
        bot, store, clock, transport, seen = build(tmp_path, responder)
        await bot.start()
        try:
            await bot.step([msg(721, "等下再说")])
            clock.advance(5)
            await bot.autonomous_step(force=True)
            clock.advance(3600 * 24 * 365 * 10)

            # Five scheduler passes with nothing new. Only the first may wake.
            for _ in range(5):
                await bot.autonomous_step(force=False)

            timer_events = store._query(
                "SELECT sequence FROM observation_events WHERE kind = 'timer'")
            assert len(timer_events) == 1, (
                f"the timer produced {len(timer_events)} wakeups instead of one"
            )
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_expired_wait_wakes_with_a_closed_window(tmp_path):
    fired = []

    def responder(request):
        if fired:
            return ModelResponse(content="", model="scripted", tool_calls=[
                ToolCall(id="c", name="finish_silently", arguments={})])
        fired.append(1)
        return ModelResponse(content="", model="scripted", tool_calls=[
            ToolCall(id="c", name="wait_for_event",
                     arguments={"purpose": "等她发日志",
                                "expires_at": "2030-01-01T00:00:00+00:00"})])

    async def scenario():
        bot, store, clock, transport, seen = build(tmp_path, responder)
        await bot.start()
        try:
            await bot.step([msg(731, "我晚点发")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            clock.advance(3600 * 24 * 365 * 10)
            await bot.autonomous_step(force=False)

            events = store._query(
                "SELECT payload_json FROM observation_events WHERE kind = 'timer'")
            assert events, "an expired window must also wake the agent"
            payload = json.loads(events[-1]["payload_json"])
            assert payload["status"] == "expired"
            assert "window" in payload["note"]

            waits = store._query("SELECT status FROM agent_waits")
            assert [w["status"] for w in waits] == ["expired"]
        finally:
            await bot.stop()
            store.close()

    run(scenario())
