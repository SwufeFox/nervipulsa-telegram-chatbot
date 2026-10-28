"""A scope whose activation just finished must not be treated as still busy.

`_active_topics` is cleaned up by a task *done-callback*, which the event loop
schedules for later. `tick()` awaited `gather`, so by the time it looked again
the finished task was often still in the dict with `.done() == True`.

The old guard was `if key in self._active_topics: continue`, so the very scope
that had just become ready was skipped - and `_batch_started` had already been
popped, so nothing re-armed it. A timer firing at that moment was lost until
some unrelated message arrived.

Found by running the real model: the `timer` observation was written and the
wait resolved, but the turn that should have reacted never ran.
"""
from __future__ import annotations

import asyncio
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


def test_a_scope_whose_task_finished_is_not_skipped(tmp_path):
    """Back-to-back turns in one scope, with no callback window in between."""
    answers = []

    async def scenario():
        def responder(request):
            answers.append(1)
            return ModelResponse(content="", model="scripted", tool_calls=[
                ToolCall(id=f"c{len(answers)}", name="send_message",
                         arguments={"text": f"第 {len(answers)} 句"})])

        bot, store, clock, transport, seen = build(tmp_path, responder)
        await bot.start()
        try:
            # First turn: two messages so the batch is followed by a leftover.
            await bot.step([msg(801, "第一句"), msg(802, "第二句")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            # A second message keeps the scope ready immediately, exactly where
            # a finished-but-not-yet-cleaned task used to hide it.
            await bot.step([msg(803, "再说点")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            assert len(answers) >= 2, (
                "the scope was skipped because its previous task had finished "
                "but had not been cleaned up yet"
            )
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_a_genuinely_running_scope_is_still_skipped(tmp_path):
    """The fix must not dispatch the same topic twice at once."""
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def scenario():
        def responder(request):
            calls.append(1)
            return ModelResponse(content="", model="scripted", tool_calls=[
                ToolCall(id="c", name="finish_silently", arguments={})])

        bot, store, clock, transport, seen = build(tmp_path, responder)
        await bot.start()
        try:
            await bot.step([msg(811, "一句话")])
            clock.advance(5)

            first = asyncio.create_task(bot.autonomy.activate(
                fixtures.TEST_CHAT, fixtures.TOPIC_A))
            await asyncio.sleep(0)  # let it take the per-topic lock
            # A second dispatch attempt for the same scope must not start.
            await bot.autonomy.tick(force=True)
            await asyncio.wait_for(first, timeout=10)
            assert len(calls) == 1, f"the same topic ran {len(calls)} times at once"
        finally:
            await bot.stop()
            store.close()

    run(scenario())
