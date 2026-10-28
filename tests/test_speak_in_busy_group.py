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
            assert "conversation moved on" in payload["note"]
            assert payload["text"] == "被丢掉的话"
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_default_tolerance_scales_with_how_fast_the_room_moves(tmp_path):
    """A quiet topic keeps a tight bound; a busy one is not held to impossible."""
    async def scenario():
        bot, store, clock, transport = build(tmp_path, lambda request: ModelResponse(
            content="", model="scripted",
            tool_calls=[ToolCall(id="c", name="finish_silently", arguments={})]))
        loop = bot.autonomy
        await bot.start()
        try:
            quiet = loop.recent_scope_rate(fixtures.TEST_CHAT, fixtures.TOPIC_A)
            assert quiet == 0, quiet

            # Roughly one message every 2.5s across the rate window.
            for i in range(60):
                bot._ingest(msg(900 + i, f"消息 {i}"), TickStats())
                clock.advance(2.5)

            busy = loop.recent_scope_rate(fixtures.TEST_CHAT, fixtures.TOPIC_A)
            assert busy > quiet, "a busy scope must get more tolerance than a quiet one"
            assert busies_are_bounded(busy), busy
        finally:
            await bot.stop()
            store.close()

    def busies_are_bounded(value):
        return 0 <= value <= 100

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
