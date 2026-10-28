"""A live group exposed a bug that made her mute without ever noticing.

The group produced a message every ~3 seconds; a thinking turn took 56-88
seconds. `send_message` defaulted to `freshness="strict"`, which cancels the
send if ANY newer message arrived - so in a normal conversation every reply was
discarded. Measured on the real database: two speak decisions in the group,
both cancelled with `needs_refresh`, zero delivered. The model was never told
that `strict` was the default, so it could not even choose otherwise.
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
from app.autonomous_tools import AutonomousToolCatalog
from app.bot import GroupAgent, TickStats
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
    """A real bot loop; `responder` may ingest messages mid-call."""
    clock = FakeClock()
    store = Store(tmp_path / "app.db", clock=clock)
    transport = FakeTransport()
    prompt = load_default_profile()
    settings = Settings(model="scripted", workspace=str(tmp_path / "rt"),
                        max_timeout=30, default_timeout=30, context_limit=40000)

    def factory(spec):
        return NervipulsaAgent(
            settings=settings, workspace=spec.workspace, system_prompt=prompt,
            backend=ScriptedBackend(responder), timeout=10,
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
    return bot, store, clock, transport


def chatter_during_turn(bot, uid, texts):
    """Ingest messages as if they arrived while the model was thinking.

    `_ingest` is the real ingest path and is synchronous, so calling it from
    inside the responder puts the new observations exactly where they actually
    land: after the batch was frozen, before the send is claimed.
    """
    def responder(request):
        for offset, text in enumerate(texts):
            bot._ingest(msg(uid + offset, text), TickStats())
        return ModelResponse(
            content="", model="scripted",
            tool_calls=[ToolCall(id="c", name="send_message",
                                 arguments={"text": "这个我知道"})],
        )
    return responder


def test_reply_survives_the_chatter_that_arrives_while_thinking(tmp_path):
    """The exact live failure: a few newer messages must not kill the reply."""
    async def scenario():
        bot, store, clock, transport = build(tmp_path, None)
        bot = None  # built below, needs the bot instance inside the responder
        return

    async def real():
        holder = {}

        def responder(request):
            for offset, text in enumerate(["我也想知道", "蹲一个", "楼上说得对"]):
                holder["bot"]._ingest(msg(802 + offset, text), TickStats())
            return ModelResponse(
                content="", model="scripted",
                tool_calls=[ToolCall(id="c", name="send_message",
                                     arguments={"text": "这个我知道"})],
            )

        bot, store, clock, transport = build(tmp_path, responder)
        holder["bot"] = bot
        await bot.start()
        try:
            await bot.step([msg(801, "有人用过 ClickHouse 吗")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            assert [s["text"] for s in transport.sends] == ["这个我知道"], (
                "the reply was dropped just because people kept chatting"
            )
        finally:
            await bot.stop()
            store.close()

    run(real())


def test_strict_still_discards_when_explicitly_asked(tmp_path):
    """`strict` must keep meaning strict - it is now an informed choice."""
    async def scenario():
        holder = {}

        def responder(request):
            holder["bot"]._ingest(msg(812, "啊算了，我自己看"), TickStats())
            return ModelResponse(
                content="", model="scripted",
                tool_calls=[ToolCall(id="c", name="send_message",
                                     arguments={"text": "旧答案",
                                                "freshness": "strict"})],
            )

        bot, store, clock, transport = build(tmp_path, responder)
        holder["bot"] = bot
        await bot.start()
        try:
            await bot.step([msg(811, "现在几点")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            assert transport.sends == []
            row = store._query("SELECT status, error FROM actions")[0]
            assert row["status"] == "cancelled"
            assert "needs_refresh" in (row["error"] or "")
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_dropped_reply_is_reported_back_to_the_agent(tmp_path):
    """Silence after a cancelled send is how she went mute for 20 minutes."""
    async def scenario():
        holder = {}

        def responder(request):
            holder["bot"]._ingest(msg(822, "第二句"), TickStats())
            return ModelResponse(
                content="", model="scripted",
                tool_calls=[ToolCall(id="c", name="send_message",
                                     arguments={"text": "被丢掉的话",
                                                "freshness": "strict"})],
            )

        bot, store, clock, transport = build(tmp_path, responder)
        holder["bot"] = bot
        await bot.start()
        try:
            await bot.step([msg(821, "第一句")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            rows = store._query(
                "SELECT payload_json FROM observation_events WHERE kind='action_result'")
            assert rows, "a dropped reply left no trace the agent could see"
            payload = json.loads(rows[-1]["payload_json"])
            assert payload["status"] == "cancelled"
            assert "needs_refresh" in payload["error"]
            assert "nobody saw this" in payload["note"]
            assert payload["text"] == "被丢掉的话"
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_default_reply_is_guarded_by_time_not_by_message_count(tmp_path):
    """The default must not encode a guess about how chatty a room is.

    A previous version derived a message-count tolerance from the trailing
    arrival rate. Live data broke it: 13 messages arrived during one turn where
    the average predicted 4, so a current reply was still dropped. The guard is
    now a deadline, which is what "too late" actually means.
    """
    async def scenario():
        bot, store, clock, transport = build(tmp_path, lambda request: ModelResponse(
            content="", model="scripted",
            tool_calls=[ToolCall(id="c", name="finish_silently", arguments={})]))
        await bot.start()
        try:
            assert bot.autonomy.default_reply_ttl == 180.0

            await bot.step([msg(900, "在吗")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            rows = store._query("SELECT 1 FROM actions")
            assert rows == [], "finish_silently must not create a send intent"
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_schema_explains_freshness_instead_of_hiding_the_default():
    """The model could not choose wisely about a knob nobody described."""
    catalog = AutonomousToolCatalog()
    schema = next(s for s in catalog.schemas(workspace=".")
                  if s["function"]["name"] == "send_message")
    properties = schema["function"]["parameters"]["properties"]
    freshness = properties["freshness"]

    assert set(freshness["enum"]) == {"default", "strict", "bounded"}
    description = freshness["description"].lower()
    assert "default" in description
    assert "strict" in description
    assert "discard" in description
    assert "bounded" in properties["max_unobserved_events"]["description"].lower()


def test_a_burst_does_not_kill_the_reply_but_old_age_does(tmp_path):
    """Measured live: 13 messages arrived where the trailing average said 4.

    Counting alone cannot tell a burst from a topic change, so the default is
    bounded by time: a burst is survivable, a three-minute-old reply is not.
    """
    async def scenario():
        holder = {}

        def responder(request):
            for offset in range(12):  # a real burst, bigger than any estimate
                holder["bot"]._ingest(msg(860 + offset, f"刷屏 {offset}"), TickStats())
            return ModelResponse(
                content="", model="scripted",
                tool_calls=[ToolCall(id="c", name="send_message",
                                     arguments={"text": "还得看我"})],
            )

        bot, store, clock, transport = build(tmp_path, responder)
        holder["bot"] = bot
        await bot.start()
        try:
            await bot.step([msg(850, "有人懂这个吗")])
            clock.advance(5)
            await bot.autonomous_step(force=True)
            assert [s["text"] for s in transport.sends] == ["还得看我"], (
                "a burst cancelled an otherwise current reply"
            )
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_a_reply_that_grew_too_old_is_dropped_and_reported(tmp_path):
    """The deadline is what protects against a stale reply, so it must report."""
    async def scenario():
        bot, store, clock, transport = build(tmp_path, lambda request: ModelResponse(
            content="", model="scripted",
            tool_calls=[ToolCall(id="c", name="finish_silently", arguments={})]))
        await bot.start()
        try:
            # An intent that has already missed its deadline (store clock, not
            # wall clock: the test store runs on a fake clock), as happens when
            # the send budget or a slow turn holds it too long.
            store.prepare_action(
                action_id="late", workflow_id=None, chat_id=fixtures.TEST_CHAT,
                topic_id=fixtures.TOPIC_A, text="迟到的回答", observed_through=0,
                freshness="bounded", max_unobserved=100,
                expires_at=clock() - 1,
            )
            await bot.autonomy.flush_actions()

            assert transport.sends == [], "an expired reply must not be sent"
            row = store.get_action("late")
            assert row["status"] == "cancelled"
            assert "expired_unexecuted" in (row["error"] or "")

            events = store._query(
                "SELECT payload_json FROM observation_events WHERE kind='action_result'"
                " AND json_extract(payload_json, '$.action_id') = 'late'")
            assert events, "an expired reply left no trace the agent could see"
            payload = json.loads(events[-1]["payload_json"])
            assert payload["status"] == "cancelled"
            assert "waited too long" in payload["error"]
            assert "nobody saw this" in payload["note"]
        finally:
            await bot.stop()
            store.close()

    run(scenario())
