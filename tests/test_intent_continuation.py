"""The goal set in one activation is visible in the next one.

This is the difference between "woken up to make a fresh decision" and
"continuing what I was doing". The store tests cover persistence; these cover
the wire into the model, which is the part that was missing entirely.
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
from app.autonomous_tools import AutonomousToolCatalog
from app.bot import GroupAgent
from app.config import AppConfig, load_default_profile
from app.store import Store
from app.telegram import TelegramClient
from app.turns import TurnManager
from conftest import FakeClock, FakeTransport, run


def msg(uid, text, sender_id=fixtures.ALICE, topic=None, name="alice"):
    return fixtures.message(update_id=uid, message_id=uid, sender_id=sender_id,
                            username=name, topic_id=topic or fixtures.TOPIC_A,
                            text=text)


def build(tmp_path, answer):
    """`answer(turn_index, payload)` returns a ModelResponse."""
    clock = FakeClock()
    store = Store(tmp_path / "app.db", clock=clock)
    transport = FakeTransport()
    prompt = load_default_profile()
    settings = Settings(model="scripted", workspace=str(tmp_path / "rt"),
                        max_timeout=30, default_timeout=30, context_limit=40000)
    payloads: list[dict] = []
    requests: list[str] = []
    # Number model responses from one across all activations.
    calls = 0

    def factory(spec):
        def responder(request):
            nonlocal calls
            requests.append(json.dumps(request.messages, ensure_ascii=False))
            payload = payloads[-1] if payloads else {}
            calls += 1
            return answer(calls, payload)

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
                     turn_manager=TurnManager(debounce_seconds=0, clock=clock),
                     bot_user_id=fixtures.ALMA, clock=clock)

    # Record exactly what the loop hands to the model. Patching here (instead of
    # scraping the prompt text) keeps the assertion about the wire payload, which
    # is the thing that was missing.
    loop = bot.autonomy
    original = loop._render

    def render(observations, chat_id, topic_id, **kwargs):
        rendered = original(observations, chat_id, topic_id, **kwargs)
        payloads.append(rendered)
        return rendered

    loop._render = render
    bot.provider_requests = requests
    return bot, store, clock, transport, payloads


def call(name, **arguments):
    return ModelResponse(content="", model="scripted",
                         tool_calls=[ToolCall(id=f"c-{name}", name=name,
                                              arguments=arguments)])


def test_a_goal_set_in_one_turn_is_shown_in_the_next(tmp_path):
    turns = []

    def answer(index, payload):
        turns.append(payload)
        if index == 1:
            return call("set_intent", goal="等林贴出日志，看是不是 Code 62")
        if index == 2:
            return call("wait_for_event", purpose="等日志")
        return call("finish_silently")

    async def scenario():
        bot, store, clock, transport, payloads = build(tmp_path, answer)
        await bot.start()
        try:
            await bot.step([msg(101, "我把日志贴群里了")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            # The goal is durable after the turn that set it.
            assert store.current_intent(fixtures.TEST_CHAT, fixtures.TOPIC_A) is not None

            # A later, unrelated activation must see it.
            await bot.step([msg(102, "有人吗")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            assert len(turns) >= 2, "the second turn never ran"
            shown = turns[-1].get("open_intent")
            assert shown is not None, "the goal was not rendered into the payload"
            assert "Code 62" in shown["goal"]
            assert "Code 62" in bot.provider_requests[-1]
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_no_goal_renders_as_null_not_missing(tmp_path):
    """An absent goal must be explicit: the model should not guess."""
    seen = []

    def answer(index, payload):
        seen.append(payload)
        return call("finish_silently")

    async def scenario():
        bot, store, clock, transport, payloads = build(tmp_path, answer)
        await bot.start()
        try:
            await bot.step([msg(201, "在吗")])
            clock.advance(5)
            await bot.autonomous_step(force=True)
            assert seen
            assert "open_intent" in seen[0]
            assert seen[0]["open_intent"] is None
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_setting_a_goal_twice_replaces_it(tmp_path):
    def answer(index, payload):
        goals = ["第一版目标", "第二版目标"]
        if index <= len(goals):
            return call("set_intent", goal=goals[index - 1])
        return call("finish_silently")

    async def scenario():
        bot, store, clock, transport, payloads = build(tmp_path, answer)
        await bot.start()
        try:
            await bot.step([msg(301, "一")])
            clock.advance(5)
            await bot.autonomous_step(force=True)
            await bot.step([msg(302, "二")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            rows = store._query("SELECT goal, status FROM agent_intents ORDER BY rowid")
            assert [(row["goal"], row["status"]) for row in rows] == [
                ("第一版目标", "abandoned"), ("第二版目标", "open")]
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_a_goal_closes_only_after_the_message_is_delivered(tmp_path):
    """Closing on acceptance would end a goal for a reply that never landed."""

    def answer(index, payload):
        if index == 1:
            return call("set_intent", goal="要回他一句")
        if index == 2:
            return call("send_message", text="回你了", resolves_intent=True)
        return call("finish_silently")

    async def scenario():
        bot, store, clock, transport, payloads = build(tmp_path, answer)
        await bot.start()
        try:
            await bot.step([msg(401, "你怎么看")])
            clock.advance(5)
            await bot.autonomous_step(force=True)
            await bot.step([msg(402, "说话啊")])
            clock.advance(5)
            result = await bot.autonomous_step(force=True)

            # The intent id must have been written with the action, or a crash
            # after delivery could never close it.
            rows = store._query("SELECT intent_id, status FROM actions")
            assert rows and rows[0]["intent_id"], "the action did not record its goal"

            # Delivered, so the goal is now closed.
            assert transport.sends, "nothing was delivered"
            assert store.current_intent(fixtures.TEST_CHAT, fixtures.TOPIC_A) is None
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_a_cancelled_reply_leaves_the_goal_open(tmp_path):
    store = Store(tmp_path / "app.db")
    try:
        assert store.open_intent(intent_id="i", chat_id=1, topic_id=None, goal="reply")
        store.prepare_action(action_id="a", workflow_id=None, chat_id=1, topic_id=None,
                             text="reply", observed_through=0, intent_id="i")
        store.append_synthetic_observation(dedupe_key="new", chat_id=1, topic_id=None,
                                           kind="message", payload={})
        assert store.claim_action("a") == "stale"
        assert store.finish_action("a", "cancelled", error="needs refresh")
        assert store.get_action("a")["status"] == "cancelled"
        assert store.current_intent(1, None)["intent_id"] == "i"
    finally:
        store.close()


def test_the_tool_reports_what_it_did(tmp_path):
    """The receipt shape is what the model reads back; it must not lie."""
    catalog = AutonomousToolCatalog()

    def validate(name, arguments):
        tool = catalog.get(name)
        assert tool is not None
        return tool.validate(arguments, max_timeout=30)

    assert validate("set_intent", {"goal": "做一件小事"}) is None
    assert validate("set_intent", {"goal": ""}) == "invalid_payload"
    assert validate("set_intent", {"goal": "x", "extra": 1}) == "invalid_payload"
    assert validate("set_intent", {"goal": "x", "expires_at": "17:00"}) == "invalid_payload"
    assert validate("set_intent", {"goal": "x", "expires_at": "2030-01-01T00:00:00+08:00"}) is None

    assert validate("resolve_intent", {}) is None
    assert validate("resolve_intent", {"abandoned": True}) is None
    assert validate("resolve_intent", {"abandoned": "yes"}) == "invalid_payload"
    assert validate("resolve_intent", {"nope": 1}) == "invalid_payload"
