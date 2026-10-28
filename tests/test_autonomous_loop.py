"""End-to-end behaviour of the autonomous participation loop.

The assertions are about *who decides*, not reply quality:

* an ordinary message with no mention reaches the real Runtime and the model,
* the model's tool call becomes a durable intent, and only the outbox sends it,
* `finish_silently` is a normal completion, not a failure,
* an empty provider response is an error, never mistaken for silence,
* a new observation invalidates a stale strict action instead of sending it,
* a 'sending' action that outlived its process is never resent,
* concurrent topics do not supersede each other.

The fake model still runs through the real Nervipulsa Runtime, so these tests
exercise the event admission, projection, activation and tool dispatch paths
the production bot uses.
"""
from __future__ import annotations

import asyncio
import json
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
from nervipulsa.runtime import Runtime

from app import fixtures
from app.agent import NervipulsaAgent
from app.bot import GroupAgent
from app.config import AppConfig, load_default_profile
from app.store import Store
from app.telegram import TelegramClient
from app.turns import TurnManager
from conftest import FakeClock, FakeTransport, run


def update(mid=701, topic=None, text="A deployment is leaking memory"):
    return fixtures.message(
        update_id=mid, message_id=mid, sender_id=fixtures.ALICE, username="alice",
        topic_id=fixtures.TOPIC_A if topic is None else topic, text=text,
    )


def tool_call(name, **arguments):
    return ModelResponse(content="", model="scripted",
                         tool_calls=[ToolCall(id="call-1", name=name, arguments=arguments)])


def build(tmp_path: Path, responder, *, transport=None, idle_seconds=0.0):
    clock = FakeClock()
    store = Store(tmp_path / "app.db")
    transport = transport or FakeTransport()
    prompt = load_default_profile()
    config = AppConfig(
        bot_name="Alma", bot_username="alma_bot",
        allowed_chat_ids=(fixtures.TEST_CHAT,), dry_run=False,
        debounce_seconds=2.0, idle_seconds=idle_seconds, workspace=str(tmp_path),
        model="scripted",
    )
    settings = Settings(model="scripted", workspace=str(tmp_path / "runtime"),
                        max_timeout=30, default_timeout=30, context_limit=40000)
    agent = None

    def factory(spec):
        # Mirror the production factory: the catalog and the commit hook are
        # per-scope and must reach the runtime at construction.
        nonlocal agent
        agent = NervipulsaAgent(
            settings=settings, workspace=spec.workspace, system_prompt=prompt,
            backend=ScriptedBackend(responder), timeout=10,
            tool_catalog=spec.tool_catalog,
            response_commit_hook=spec.response_commit_hook,
        )
        return agent

    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=prompt, agent_factory=factory,
                     turn_manager=TurnManager(debounce_seconds=2, clock=clock),
                     bot_user_id=fixtures.ALMA, clock=clock)

    class Handle:
        """Lazy view of the agent the factory creates on first activation."""
        @property
        def runtime(self):
            return agent.runtime if agent is not None else None

        @property
        def agent(self):
            return agent

    return bot, store, clock, transport, Handle()


def send_rows(db: Path):
    with sqlite3.connect(db) as con:
        return con.execute("SELECT status, text FROM actions ORDER BY created_at").fetchall()


def test_unmentioned_message_reaches_the_model_and_its_tool_sends(tmp_path):
    async def scenario():
        bot, store, clock, transport, agent = build(
            tmp_path, lambda request: tool_call("send_message", text="Check the heap"))
        try:
            await bot.start()
            await bot.step([update(text="A deployment is leaking memory")])
            clock.advance(5)
            await bot.autonomous_step()
            assert bot._agent_failures == {}, bot._agent_failures
            assert isinstance(agent.runtime, Runtime), "runtime was not started"
            request = json.loads(json.dumps(agent.runtime.actor.activations[0].snapshot))
            assert "leaking memory" in json.dumps(request, ensure_ascii=False)
            names = [s["function"]["name"] for s in agent.runtime.actor.tools]
            assert set(names) == {"send_message", "finish_silently", "wait_for_event",
                                  "wait_until", "update_state"}
            assert [s["text"] for s in transport.sends] == ["Check the heap"]
            assert send_rows(tmp_path / "app.db") == [("sent", "Check the heap")]
        finally:
            await bot.stop()
            store.close()
    run(scenario())


def test_mention_can_finish_without_speaking(tmp_path):
    async def scenario():
        bot, store, clock, transport, agent = build(
            tmp_path, lambda request: tool_call("finish_silently", reason="already solved"))
        try:
            await bot.start()
            await bot.step([update(text="@alma_bot already solved, thanks")])
            clock.advance(5)
            results = await bot.autonomous_step()
            assert results and results[0].outcome == "silent"
            assert transport.sends == [], "a mention must not force a message"
        finally:
            await bot.stop()
            store.close()
    run(scenario())


def test_empty_model_response_is_an_error_not_silence(tmp_path):
    async def scenario():
        bot, store, clock, transport, agent = build(
            tmp_path, lambda request: ModelResponse(content="", model="scripted"))
        try:
            await bot.start()
            await bot.step([update()])
            clock.advance(5)
            results = await bot.autonomous_step()
            assert results and results[0].state == "failed"
            assert results[0].outcome != "silent"
            assert transport.sends == []
        finally:
            await bot.stop()
            store.close()
    run(scenario())


def test_strict_action_is_not_sent_when_new_observations_arrive(tmp_path):
    """The real race: a message lands while the model is still thinking.

    Sending `stale answer` here would mean the bot replied to a question the
    group had already resolved. The action was decided against sequence N; by
    delivery time the topic is at N+1, so a strict intent must not fire.
    """
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def responder(request):
            entered.set()
            await release.wait()
            return tool_call("send_message", text="stale answer")

        bot, store, clock, transport, agent = build(tmp_path, responder)
        try:
            await bot.start()
            await bot.step([update(mid=701, text="first question")])
            clock.advance(5)
            thinking = asyncio.create_task(bot.autonomous_step())
            await asyncio.wait_for(entered.wait(), 5)

            # New information arrives mid-inference. Ingest must not block.
            await bot.step([update(mid=702, text="actually never mind, fixed")])

            release.set()
            await asyncio.wait_for(thinking, 10)

            assert transport.sends == [], "stale action must not be delivered"
            rows = send_rows(tmp_path / "app.db")
            assert rows and rows[0][0] == "cancelled"
        finally:
            release.set()
            await bot.stop()
            store.close()
    run(scenario())


def test_sending_at_crash_becomes_unknown_and_is_not_retried(tmp_path):
    db = tmp_path / "app.db"
    store = Store(db)
    store.prepare_action(action_id="act_crash", workflow_id=None, chat_id=fixtures.TEST_CHAT,
                         topic_id=None, text="hi", observed_through=0)
    assert store.claim_action("act_crash") == "claimed"
    store.close()

    reopened = Store(db)
    assert reopened.recover_orphaned_actions() == ["act_crash"]
    row = reopened.get_action("act_crash")
    assert row["status"] == "unknown"
    assert reopened.pending_actions() == []
    reopened.close()


def test_wait_is_durable_and_wakes_on_a_matching_event(tmp_path):
    """A wait survives the turn that created it and is satisfied by the next.

    It must not fire on its own batch: the agent registered it while looking at
    those messages, so treating them as the awaited event would make every
    wait self-satisfying.
    """
    async def scenario():
        seen = {"n": 0}

        def responder(request):
            seen["n"] += 1
            if seen["n"] == 1:
                return tool_call("wait_for_event", purpose="see the follow-up",
                                 event_types=["message"])
            return tool_call("send_message", text="saw the follow-up")

        bot, store, clock, transport, agent = build(tmp_path, responder)
        try:
            await bot.start()
            await bot.step([update(mid=701, text="investigating")])
            clock.advance(5)
            await bot.autonomous_step()
            with sqlite3.connect(tmp_path / "app.db") as con:
                waits = con.execute(
                    "SELECT kind, purpose, status FROM agent_waits").fetchall()
            assert waits == [("event", "see the follow-up", "active")]
            assert transport.sends == []

            await bot.step([update(mid=702, text="here is the follow-up")])
            clock.advance(5)
            await bot.autonomous_step()
            with sqlite3.connect(tmp_path / "app.db") as con:
                waits = con.execute(
                    "SELECT status FROM agent_waits").fetchall()
            assert waits == [("satisfied",)]
            assert [s["text"] for s in transport.sends] == ["saw the follow-up"]
        finally:
            await bot.stop()
            store.close()
    run(scenario())


def test_topics_are_independent(tmp_path):
    async def scenario():
        calls = []

        def responder(request):
            calls.append(1)
            topic = "A" if len(calls) == 1 else "B"
            return tool_call("send_message", text=f"reply {topic}")

        bot, store, clock, transport, agent = build(tmp_path, responder)
        try:
            await bot.start()
            await bot.step([
                update(mid=701, topic=fixtures.TOPIC_A, text="topic A question"),
                update(mid=702, topic=fixtures.TOPIC_B, text="topic B question"),
            ])
            clock.advance(5)
            await bot.autonomous_step()
            assert len(transport.sends) == 2
        finally:
            await bot.stop()
            store.close()
    run(scenario())


def test_agent_state_is_scoped_and_read_back(tmp_path):
    async def scenario():
        state = {"turn": 0}

        def responder(request):
            state["turn"] += 1
            if state["turn"] == 1:
                return ModelResponse(content="", model="scripted", tool_calls=[
                    ToolCall(id="c1", name="update_state",
                             arguments={"key": "open_issue", "value": "memory leak"}),
                    ToolCall(id="c2", name="finish_silently", arguments={}),
                ])
            return tool_call("finish_silently", reason="noted")

        bot, store, clock, transport, agent = build(tmp_path, responder)
        try:
            await bot.start()
            await bot.step([update(mid=701, text="there is a memory leak")])
            clock.advance(5)
            await bot.autonomous_step()
            assert store.get_state(fixtures.TEST_CHAT, fixtures.TOPIC_A) == {
                "open_issue": "memory leak"}
            await bot.step([update(mid=702, text="any update?")])
            clock.advance(5)
            await bot.autonomous_step()
            last = json.loads(json.dumps(agent.runtime.actor.activations[-1].snapshot))
            assert "memory leak" in json.dumps(last, ensure_ascii=False)
        finally:
            await bot.stop()
            store.close()
    run(scenario())
