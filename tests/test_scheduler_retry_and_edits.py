"""Two scheduling gaps a live run exposed.

1. A failed turn cleared the batch window before calling the agent and never
   re-armed it. Its observations were still past the cursor, but nothing would
   look at them again until an unrelated message arrived or the process
   restarted.
2. An edited message was recorded as a `message_edit` observation, but
   `record_inbound_message` reported False for it (the message row already
   exists) and the ingest path read that as "duplicate", so the new text sat in
   the stream with nothing scheduled to read it.
"""
from __future__ import annotations

import asyncio
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
            session_id=spec.session_id, restore=spec.restore,
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


def test_failed_turn_is_requeued_with_a_real_backoff(tmp_path):
    """A failed turn must not strand its observation until the next message.

    Two things are checked: the scope comes back by itself, and the retry does
    not spin - a decided "paused" answer from Core is not retried as eagerly as
    a transient failure.
    """
    async def scenario():
        bot, store, clock, _, _ = build(tmp_path)
        await bot.start()
        try:
            await bot.step([msg(901, "有人知道这个报错吗")])
            clock.advance(5)

            loop = bot.autonomy
            scope = (fixtures.TEST_CHAT, fixtures.TOPIC_A)

            loop._requeue_after_failure(*scope)
            assert scope in loop._batch_started, "a failed turn dropped its scope"
            assert loop._retry_after[scope] > loop._monotonic()

            transient = loop._retry_after[scope] - loop._monotonic()
            loop._requeue_after_failure(*scope, activation_status="paused")
            paused = loop._retry_after[scope] - loop._monotonic()
            assert paused > transient, (
                "a paused actor must not be retried as eagerly as a transient failure"
            )
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_paused_actor_is_not_hammered_by_the_scheduler(tmp_path):
    """Core refuses to resume `response_unknown` on new input; honour that."""
    attempts = []

    def responder(request):
        attempts.append(1)
        raise RuntimeError("provider went away")

    async def scenario():
        bot, store, clock, _, handle = build(tmp_path, responder)
        await bot.start()
        try:
            await bot.step([msg(902, "在吗")])
            clock.advance(5)
            first = await bot.autonomous_step(force=True)
            assert first and first[0].state == "failed", first

            # Core paused on `response_unknown`; the scope is still queued so a
            # restart or an operator retry can resume it.
            assert bot.queued_topics(), "a paused scope was dropped"
            scope = (fixtures.TEST_CHAT, fixtures.TOPIC_A)
            assert bot.autonomy._retry_after[scope] - bot.autonomy._monotonic() >= 300

            for _ in range(5):
                clock.advance(1)
                await bot.autonomous_step(force=False)
            assert len(attempts) == 1, "the scheduler retried a paused actor in a loop"
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_edited_message_wakes_the_topic(tmp_path):
    """An edit is new information; the old text is not re-delivered as new."""
    async def scenario():
        bot, store, clock, _, _ = build(tmp_path)
        await bot.start()
        try:
            await bot.step([msg(911, "3 点开会")])
            clock.advance(5)
            await bot.autonomous_step(force=True)
            assert not store.pending_observations(fixtures.TEST_CHAT, fixtures.TOPIC_A)

            # Telegram sends an edit as a new update id carrying the SAME
            # message_id; only the text changed.
            edit = msg(911, "3 点开会，改到 4 点")
            edit["update_id"] = 912
            edit["edited_message"] = edit.pop("message")
            await bot.step([edit])

            pending = store.pending_observations(fixtures.TEST_CHAT, fixtures.TOPIC_A)
            assert len(pending) == 1, pending
            assert pending[0]["kind"] == "message_edit"
            assert bot.queued_topics(), "an edit did not re-arm the topic"

            clock.advance(5)
            results = await bot.autonomous_step(force=False)
            assert results and results[0].state == "completed", results
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_a_duplicate_update_is_still_dropped(tmp_path):
    """The edit fix must not turn a redelivered update into new work."""
    async def scenario():
        bot, store, clock, _, _ = build(tmp_path)
        await bot.start()
        try:
            await bot.step([msg(921, "hello")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            tick, _ = await bot.step([msg(921, "hello")])
            assert tick.duplicates == 1
            assert not store.pending_observations(fixtures.TEST_CHAT, fixtures.TOPIC_A)
            assert bot.queued_topics() == []
        finally:
            await bot.stop()
            store.close()

    run(scenario())
