"""A turn that calls no tool has not decided anything - and must not be
recorded as if it had.

`AutonomousLoop` used to fall back to `outcome="no_action"`: the application
supplied a conclusion the model never reached, and the model's text (which is
never delivered) vanished with it. Silence has to be something the agent
chooses with `finish_silently`, not something inferred from a missing tool call.
"""
from __future__ import annotations

import json
import sys
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


def text_only(request=None):
    """A response with prose and no tool call. Takes the request, ignores it."""
    return ModelResponse(content="这个我知道答案", model="scripted")


def test_text_without_a_tool_call_is_not_recorded_as_silence(tmp_path):
    """One reminder, then an honest `undecided` - never a fabricated `no_action`."""
    async def scenario():
        bot, store, clock, transport, seen = build(tmp_path, text_only)
        await bot.start()
        try:
            await bot.step([msg(701, "你知道这个吗")])
            clock.advance(5)
            results = await bot.autonomous_step(force=True)

            # It asked once more rather than giving up on the first attempt.
            assert len(seen) >= 2, "the model was never asked to actually decide"
            # Core wraps it as a runtime event; the payload carries the ask.
            envelope = json.loads(seen[-1].messages[-1]["content"])
            reminder = envelope["payload"]
            assert envelope["type"] == "runtime.observation"
            assert reminder["kind"] == "decision_required"
            assert "did not call any tool" in reminder["note"]

            assert results and results[0].outcome == "undecided", results
            assert transport.sends == []

            # Recorded with a reason, but NOT queued as new input: an
            # observation here would wake her again and a prose-only model
            # would cycle forever.
            skips = store._query("SELECT * FROM observation_skips")
            assert skips, "an undecided turn was recorded silently"
            assert "no tool call" in skips[-1]["reason"]
            assert not store._query(
                "SELECT 1 FROM observation_events WHERE kind = 'undecided'")
            assert store.pending_observations(fixtures.TEST_CHAT, fixtures.TOPIC_A) == []
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_the_reminder_can_rescue_the_turn(tmp_path):
    """Text first, then a real tool call: the turn ends as the agent chose."""
    attempts = []

    def responder(request):
        attempts.append(request)
        if len(attempts) == 1:
            return text_only("嗯……")
        return ModelResponse(content="", model="scripted", tool_calls=[
            ToolCall(id="c", name="send_message", arguments={"text": "知道"})])

    async def scenario():
        bot, store, clock, transport, seen = build(tmp_path, responder)
        await bot.start()
        try:
            await bot.step([msg(711, "在吗")])
            clock.advance(5)
            results = await bot.autonomous_step(force=True)

            assert [s["text"] for s in transport.sends] == ["知道"]
            assert results and results[0].outcome == "speak", results
            assert not store._query(
                "SELECT 1 FROM observation_events WHERE kind = 'undecided'")
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_finish_silently_is_not_treated_as_undecided(tmp_path):
    """An explicit choice of silence must pass through untouched."""
    def responder(request):
        return ModelResponse(content="", model="scripted", tool_calls=[
            ToolCall(id="c", name="finish_silently", arguments={})])

    async def scenario():
        bot, store, clock, transport, seen = build(tmp_path, responder)
        await bot.start()
        try:
            await bot.step([msg(721, "嗯")])
            clock.advance(5)
            results = await bot.autonomous_step(force=True)

            assert results and results[0].outcome == "silent", results
            assert len(seen) == 1, "an explicit silence must not trigger a reminder"
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_state_only_turn_is_recorded_as_noted(tmp_path):
    """Writing a note down is a decision, even though it sends nothing."""
    def responder(request):
        return ModelResponse(content="", model="scripted", tool_calls=[
            ToolCall(id="c", name="update_state",
                     arguments={"key": "tone", "value": "她在生气"})])

    async def scenario():
        bot, store, clock, transport, seen = build(tmp_path, responder)
        await bot.start()
        try:
            await bot.step([msg(731, "别理我")])
            clock.advance(5)
            results = await bot.autonomous_step(force=True)

            assert results and results[0].outcome == "noted", results
            assert len(seen) == 1
            assert transport.sends == []
        finally:
            await bot.stop()
            store.close()

    run(scenario())
