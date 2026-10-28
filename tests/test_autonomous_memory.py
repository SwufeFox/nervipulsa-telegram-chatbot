"""The agent remembers the conversation across a restart.

Before this, the Runtime's transcript lived only in memory: the process held
what had been said, and a restart silently produced an agent that had never
met the group. The application's SQLite knew the messages, but nothing put
them back in front of the model.

These tests use the real Runtime and a fake provider. They check that the
second process actually *sees* the first one's conversation in the request it
sends, not merely that some rows exist.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
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


def build(tmp_path, responder=silent):
    clock = FakeClock()
    store = Store(tmp_path / "app.db", clock=clock)
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
            session_id=spec.session_id,
            restore=spec.restore,
        )
        return handle["agent"]

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
    return bot, store, clock, transport, handle


def body_of(request):
    return json.dumps(request.messages, ensure_ascii=False)


def test_second_process_sees_the_first_process_conversation(tmp_path):
    """The whole point: a restart must not wipe what the group said."""
    seen: list = []

    def record(request):
        seen.append(request)
        return silent(request)

    async def first_run():
        bot, store, clock, _, _ = build(tmp_path, record)
        await bot.start()
        await bot.step([msg(701, "线上内存一直涨，从昨天开始")])
        clock.advance(5)
        await bot.autonomous_step()
        await bot.stop()
        store.close()

    run(first_run())
    assert seen, "the first process never called the model"

    async def second_run():
        bot, store, clock, _, handle = build(tmp_path, record)
        await bot.start()
        try:
            await bot.step([msg(702, "我查到是缓存没设上限")])
            clock.advance(5)
            await bot.autonomous_step()

            after_restart = seen[-1]
            text = body_of(after_restart)
            assert "内存一直涨" in text, (
                "after restart the model never saw the earlier conversation"
            )
            assert "缓存没设上限" in text, "the new turn was not delivered"
            report = handle["agent"].restore_report
            assert report and report["restored"] is True, report
        finally:
            await bot.stop()
            store.close()

    run(second_run())


def test_restore_is_scoped_so_topics_do_not_share_memory(tmp_path):
    """A topic must not inherit another topic's transcript."""
    seen: list = []

    def record(request):
        seen.append((request, request.messages))
        return silent(request)

    async def scenario():
        bot, store, clock, _, handle = build(tmp_path, record)
        await bot.start()
        try:
            await bot.step([msg(701, "TOPIC A secret", topic=fixtures.TOPIC_A)])
            clock.advance(5)
            await bot.autonomous_step()
            first_count = len(seen)

            await bot.step([msg(702, "TOPIC B question", topic=fixtures.TOPIC_B)])
            clock.advance(5)
            await bot.autonomous_step()

            assert len(seen) > first_count
            topic_b_text = json.dumps(seen[-1][1], ensure_ascii=False)
            assert "TOPIC B question" in topic_b_text
            assert "TOPIC A secret" not in topic_b_text, (
                "topic B's model view leaked topic A's history"
            )
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_persona_change_applies_after_restore(tmp_path):
    """Restoring must not resurrect an old system prompt."""
    seen: list = []

    def record(request):
        seen.append(request)
        return silent(request)

    async def first_run():
        clock = FakeClock()
        store = Store(tmp_path / "app.db", clock=clock)
        transport = FakeTransport()
        settings = Settings(model="scripted", workspace=str(tmp_path / "rt"),
                            max_timeout=30, default_timeout=30, context_limit=40000)
        holder = {}

        def factory(spec):
            holder["agent"] = NervipulsaAgent(
                settings=settings, workspace=spec.workspace,
                system_prompt="OLD PERSONA", backend=ScriptedBackend(record),
                timeout=10, tool_catalog=spec.tool_catalog,
                response_commit_hook=spec.response_commit_hook,
                session_id=spec.session_id, restore=spec.restore,
            )
            return holder["agent"]

        config = AppConfig(
            bot_name="Alma", bot_username="alma_bot",
            allowed_chat_ids=(fixtures.TEST_CHAT,), idle_seconds=0.0,
            workspace=str(tmp_path), model="scripted",
            min_action_interval_seconds=0.0, max_actions_per_hour=99,
        )
        bot = GroupAgent(config=config, store=store,
                         client=TelegramClient(transport),
                         system_prompt="OLD PERSONA", agent_factory=factory,
                         turn_manager=TurnManager(debounce_seconds=2, clock=clock),
                         bot_user_id=fixtures.ALMA, clock=clock)
        await bot.start()
        await bot.step([msg(701, "remember me")])
        clock.advance(5)
        await bot.autonomous_step()
        await bot.stop()
        store.close()

    run(first_run())

    async def second_run():
        clock = FakeClock()
        store = Store(tmp_path / "app.db", clock=clock)
        transport = FakeTransport()
        settings = Settings(model="scripted", workspace=str(tmp_path / "rt"),
                            max_timeout=30, default_timeout=30, context_limit=40000)
        holder = {}

        def factory(spec):
            holder["agent"] = NervipulsaAgent(
                settings=settings, workspace=spec.workspace,
                system_prompt="NEW PERSONA", backend=ScriptedBackend(record),
                timeout=10, tool_catalog=spec.tool_catalog,
                response_commit_hook=spec.response_commit_hook,
                session_id=spec.session_id, restore=spec.restore,
            )
            return holder["agent"]

        config = AppConfig(
            bot_name="Alma", bot_username="alma_bot",
            allowed_chat_ids=(fixtures.TEST_CHAT,), idle_seconds=0.0,
            workspace=str(tmp_path), model="scripted",
            min_action_interval_seconds=0.0, max_actions_per_hour=99,
        )
        bot = GroupAgent(config=config, store=store,
                         client=TelegramClient(transport),
                         system_prompt="NEW PERSONA", agent_factory=factory,
                         turn_manager=TurnManager(debounce_seconds=2, clock=clock),
                         bot_user_id=fixtures.ALMA, clock=clock)
        await bot.start()
        try:
            await bot.step([msg(702, "still there?")])
            clock.advance(5)
            await bot.autonomous_step()
            request = seen[-1]
            systems = [m for m in request.messages if m.get("role") == "system"]
            assert systems == [{"role": "system", "content": "NEW PERSONA"}]
            assert "remember me" in body_of(request)
        finally:
            await bot.stop()
            store.close()

    run(second_run())


def test_hard_exit_then_restart_keeps_memory(tmp_path):
    """A real process death, not a graceful stop, must also be recoverable."""
    db = tmp_path / "app.db"
    journal = tmp_path / "seen.jsonl"

    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(db), str(journal)],
        cwd=ROOT, timeout=90, capture_output=True, text=True,
    )
    assert child.returncode == 75, (child.returncode, child.stdout, child.stderr)
    assert journal.exists() and journal.stat().st_size > 0

    seen: list = []

    def record(request):
        seen.append(request)
        return silent(request)

    async def restart():
        bot, store, clock, _, handle = build(tmp_path, record)
        await bot.start()
        try:
            await bot.step([msg(702, "after the crash")])
            clock.advance(5)
            await bot.autonomous_step()
            text = body_of(seen[-1])
            assert "before the crash" in text, "memory lost across a hard exit"
            assert "after the crash" in text
        finally:
            await bot.stop()
            store.close()

    run(restart())


def _child(db, journal):
    """Run one turn, then die without unwinding (like a real crash)."""
    import os

    async def scenario():
        clock = FakeClock()
        store = Store(Path(db), clock=clock)
        transport = FakeTransport()
        prompt = load_default_profile()
        settings = Settings(model="scripted", workspace=str(Path(db).parent / "rt"),
                            max_timeout=30, default_timeout=30, context_limit=40000)
        seen = []

        def record(request):
            seen.append(request.messages)
            return silent(request)

        holder = {}

        def factory(spec):
            holder["agent"] = NervipulsaAgent(
                settings=settings, workspace=spec.workspace, system_prompt=prompt,
                backend=ScriptedBackend(record), timeout=10,
                tool_catalog=spec.tool_catalog,
                response_commit_hook=spec.response_commit_hook,
                session_id=spec.session_id, restore=spec.restore,
            )
            return holder["agent"]

        config = AppConfig(
            bot_name="Alma", bot_username="alma_bot",
            allowed_chat_ids=(fixtures.TEST_CHAT,), idle_seconds=0.0,
            workspace=str(Path(db).parent), model="scripted",
            min_action_interval_seconds=0.0, max_actions_per_hour=99,
        )
        bot = GroupAgent(config=config, store=store,
                         client=TelegramClient(transport),
                         system_prompt=prompt, agent_factory=factory,
                         turn_manager=TurnManager(debounce_seconds=2, clock=clock),
                         bot_user_id=fixtures.ALMA, clock=clock)
        await bot.start()
        await bot.step([msg(701, "before the crash")])
        clock.advance(5)
        await bot.autonomous_step()
        runtime = holder["agent"].runtime
        # Make sure the snapshot is on disk before dying.
        runtime.journal.flush(5)
        Path(journal).write_text(
            json.dumps({"requests": len(seen)}, ensure_ascii=False), encoding="utf-8")
        os._exit(75)

    asyncio.run(scenario())


if __name__ == "__main__":
    _child(*sys.argv[1:])
