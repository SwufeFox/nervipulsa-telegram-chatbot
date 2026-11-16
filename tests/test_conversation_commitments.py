"""Independent commitments share one topic Runtime without replacing each other."""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone

from nervipulsa.config import Settings
from nervipulsa.ports import ModelResponse, ToolCall
from nervipulsa.providers import ScriptedBackend

from app import fixtures
from app.agent import NervipulsaAgent
from app.bot import GroupAgent
from app.config import AppConfig, load_default_profile
from app.console import Console
from app.store import Store, StoredMessage
from app.telegram import TelegramClient
from conftest import FakeClock, FakeTransport, run


def response(*calls):
    return ModelResponse(tool_calls=[
        ToolCall(id=f"call-{uuid.uuid4().hex}", name=name, arguments=arguments)
        for name, arguments in calls
    ])


def message(message_id, text, *, sender=fixtures.ALICE, topic=fixtures.TOPIC_A,
            chat=fixtures.TEST_CHAT, reply_to=None):
    return fixtures.message(
        update_id=message_id, message_id=message_id, chat_id=chat, topic_id=topic,
        sender_id=sender, username="alice" if sender == fixtures.ALICE else "bob",
        text=text, reply_to_message_id=reply_to,
    )


def build(root, responder, *, clock=None, transport=None):
    clock = clock if clock is not None else FakeClock()
    transport = transport if transport is not None else FakeTransport()
    store = Store(root / "app.sqlite3", clock=clock)
    prompt = load_default_profile()
    payloads, agents = [], []

    def complete(request):
        assert request.purpose == "activation"
        envelope = json.loads(request.messages[-1]["content"])
        payload = envelope["payload"]
        if "observations" not in payload:
            return response(("finish_silently", {}))
        payloads.append(payload)
        return responder(payload)

    backend = ScriptedBackend(complete)
    config = AppConfig(
        bot_username="alma_bot", allowed_chat_ids=(fixtures.TEST_CHAT,),
        workspace=str(root), model="scripted", idle_seconds=0,
        max_batch_seconds=0, min_action_interval_seconds=0,
        max_activations_per_hour=100, max_actions_per_hour=100,
    )

    def factory(spec):
        agent = NervipulsaAgent(
            settings=Settings(model="scripted", context_limit=200_000),
            workspace=spec.workspace, system_prompt=prompt, backend=backend,
            timeout=10, tool_catalog=spec.tool_catalog,
            response_commit_hook=spec.response_commit_hook,
            session_id=spec.session_id, restore=spec.restore,
        )
        agents.append(agent)
        return agent

    bot = GroupAgent(
        config=config, store=store, client=TelegramClient(transport),
        system_prompt=prompt, agent_factory=factory,
        bot_user_id=fixtures.ALMA, clock=clock,
    )
    return bot, store, clock, transport, payloads, agents


async def deliver(bot, clock, update):
    await bot.step([update], run=False)
    clock.advance(2)
    return await bot.autonomous_step(force=True)


def test_interleaved_commitments_keep_sources_waits_and_restart_identity(tmp_path):
    def answer(payload):
        incoming = [item["payload"] for item in payload["observations"]
                    if item["type"] == "message"]
        text = incoming[-1]["text"] if incoming else ""
        if text == "Alice will bring logs":
            return response(
                ("set_intent", {"goal": "Review Alice's logs", "anchor_message_id": 101}),
                ("wait_for_event", {"purpose": "Alice's logs", "sender_id": fixtures.ALICE,
                                    "anchor_message_id": 101}),
            )
        if text == "Bob will check timezone":
            return response(
                ("set_intent", {"goal": "Check Bob's timezone", "anchor_message_id": 102}),
                ("wait_for_event", {"purpose": "Bob's config", "sender_id": fixtures.BOB,
                                    "anchor_message_id": 102}),
            )
        if text == "Bob has a partial result":
            return response(("send_message", {
                "text": "Keep checking Bob's scheduler.", "anchor_message_id": 102,
                "reply_to": 103,
            }))
        if text == "Bob confirmed UTC":
            return response(("send_message", {
                "text": "Bob's scheduler is confirmed UTC.", "anchor_message_id": 102,
                "resolves_intent": True, "reply_to": 104,
            }))
        if text == "Alice logs arrived":
            return response(("send_message", {
                "text": "Alice's logs confirm the cache issue.", "anchor_message_id": 101,
                "resolves_intent": True, "reply_to": 105,
            }))
        return response(("finish_silently", {}))

    async def scenario():
        bot, store, clock, transport, payloads, _ = build(tmp_path, answer)
        await bot.start()
        try:
            await deliver(bot, clock, message(101, "Alice will bring logs"))
            first = store.open_intents(fixtures.TEST_CHAT, fixtures.TOPIC_A)[0]
            alice_id = str(first["intent_id"])
            await deliver(bot, clock, message(102, "Bob will check timezone", sender=fixtures.BOB))
            goals = {row["anchor_message_id"]: row for row in
                     store.open_intents(fixtures.TEST_CHAT, fixtures.TOPIC_A)}
            assert set(goals) == {101, 102}
            assert goals[101]["owner_id"] == fixtures.ALICE
            assert goals[102]["owner_id"] == fixtures.BOB
            assert goals[101]["intent_id"] == alice_id
            bob_id = str(goals[102]["intent_id"])
            assert {row["intent_id"] for row in store.active_waits(
                fixtures.TEST_CHAT, fixtures.TOPIC_A)} == {alice_id, bob_id}

            await deliver(bot, clock, message(103, "Bob has a partial result",
                                               sender=fixtures.BOB, reply_to=102))
            progress = store._query("SELECT * FROM actions WHERE text = ?",
                                    ("Keep checking Bob's scheduler.",))[0]
            assert progress["intent_id"] == bob_id
            assert not progress["resolves_intent"]
            assert store.get_intent(bob_id, chat_id=fixtures.TEST_CHAT,
                                    topic_id=fixtures.TOPIC_A)["status"] == "open"

            await deliver(bot, clock, message(104, "Bob confirmed UTC",
                                               sender=fixtures.BOB, reply_to=102))
            assert [row["intent_id"] for row in store.open_intents(
                fixtures.TEST_CHAT, fixtures.TOPIC_A)] == [alice_id]
            assert {row["intent_id"] for row in store.active_waits(
                fixtures.TEST_CHAT, fixtures.TOPIC_A)} == {alice_id}
            assert store.get_intent(bob_id, chat_id=fixtures.TEST_CHAT,
                                    topic_id=fixtures.TOPIC_A)["status"] == "resolved"
        finally:
            await bot.stop()
            store.close()

        bot, store, _, _, payloads, agents = build(
            tmp_path, answer, clock=clock, transport=transport,
        )
        await bot.start()
        try:
            assert [row["intent_id"] for row in store.open_intents(
                fixtures.TEST_CHAT, fixtures.TOPIC_A)] == [alice_id]
            await deliver(bot, clock, message(105, "Alice logs arrived", reply_to=101))
            shown = payloads[-1]
            assert [(item["intent_id"], item["anchor_message_id"], item["owner_id"])
                    for item in shown["open_intents"]] == [(alice_id, 101, fixtures.ALICE)]
            incoming = [item["payload"] for item in shown["observations"]
                        if item["type"] == "message"][-1]
            assert incoming["reply_to_message_id"] == 101
            assert any(item["intent_id"] == alice_id and item["anchor_message_id"] == 101
                       for item in shown["matched_waits"])
            assert agents[0].restore_report["restored"] is True
            assert store.open_intents(fixtures.TEST_CHAT, fixtures.TOPIC_A) == []
            assert store.active_waits(fixtures.TEST_CHAT, fixtures.TOPIC_A) == []
            assert store.current_episode(fixtures.TEST_CHAT, fixtures.TOPIC_A) is None
            assert [item["text"] for item in transport.sends] == [
                "Keep checking Bob's scheduler.", "Bob's scheduler is confirmed UTC.",
                "Alice's logs confirm the cache issue.",
            ]
            assert transport.sends[-1]["reply_parameters"]["message_id"] == 105
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_linked_timer_survives_an_unrelated_conversation(tmp_path):
    clock = FakeClock()
    due_at = datetime.fromtimestamp(clock() + 60, timezone.utc).isoformat()

    def answer(payload):
        if any(item["type"] == "timer" for item in payload["observations"]):
            return response(("send_message", {
                "text": "Revisited Alice's pending issue.", "anchor_message_id": 201,
                "resolves_intent": True,
            }))
        text = payload["observations"][-1]["payload"].get("text")
        if text == "Alice needs another look later":
            return response(
                ("set_intent", {"goal": "Revisit Alice's issue", "anchor_message_id": 201}),
                ("wait_until", {"at": due_at, "purpose": "Review the issue later",
                                "anchor_message_id": 201}),
            )
        return response(("finish_silently", {}))

    async def scenario():
        bot, store, _, transport, payloads, _ = build(tmp_path, answer, clock=clock)
        await bot.start()
        try:
            await deliver(bot, clock, message(201, "Alice needs another look later"))
            intent_id = store.open_intents(fixtures.TEST_CHAT, fixtures.TOPIC_A)[0]["intent_id"]
            wait_id = store.active_waits(fixtures.TEST_CHAT, fixtures.TOPIC_A)[0]["wait_id"]
            await deliver(bot, clock, message(202, "Bob discusses another topic", sender=fixtures.BOB))
            assert [row["wait_id"] for row in store.active_waits(
                fixtures.TEST_CHAT, fixtures.TOPIC_A)] == [wait_id]
            assert transport.sends == []
            clock.advance(60)
            await bot.autonomous_step(force=False)
            timer = next(item["payload"] for item in payloads[-1]["observations"]
                         if item["type"] == "timer")
            assert timer["intent_id"] == intent_id
            assert timer["anchor_message_id"] == 201
            assert [item["text"] for item in transport.sends] == ["Revisited Alice's pending issue."]
            assert store.open_intents(fixtures.TEST_CHAT, fixtures.TOPIC_A) == []
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_foreign_topic_intent_references_cannot_mutate_or_send(tmp_path):
    references = {}

    def answer(payload):
        if payload["topic_id"] == fixtures.TOPIC_A:
            return response(
                ("set_intent", {"goal": "Alice's private topic work", "anchor_message_id": 301}),
                ("wait_for_event", {"purpose": "Alice's result", "anchor_message_id": 301,
                                    "sender_id": fixtures.ALICE}),
            )
        return response(
            ("set_intent", {"goal": "Overwrite another topic", "intent_id": references["intent"]}),
            ("send_message", {"text": "Wrong topic", "intent_id": references["intent"],
                              "resolves_intent": True}),
            ("wait_for_event", {"purpose": "Wrong topic wait", "intent_id": references["intent"]}),
            ("resolve_intent", {"intent_id": references["intent"]}),
            ("finish_silently", {}),
        )

    async def scenario():
        bot, store, clock, transport, _, agents = build(tmp_path, answer)
        await bot.start()
        try:
            await deliver(bot, clock, message(301, "Alice starts topic A"))
            original = store.open_intents(fixtures.TEST_CHAT, fixtures.TOPIC_A)[0]
            references["intent"] = str(original["intent_id"])
            await deliver(bot, clock, message(302, "Bob is in topic B", sender=fixtures.BOB,
                                               topic=fixtures.TOPIC_B))
            unchanged = store.get_intent(references["intent"], chat_id=fixtures.TEST_CHAT,
                                         topic_id=fixtures.TOPIC_A)
            assert (unchanged["status"], unchanged["goal"]) == (
                "open", "Alice's private topic work",
            )
            assert [row["intent_id"] for row in store.active_waits(
                fixtures.TEST_CHAT, fixtures.TOPIC_A)] == [references["intent"]]
            assert store.open_intents(fixtures.TEST_CHAT, fixtures.TOPIC_B) == []
            assert store.active_waits(fixtures.TEST_CHAT, fixtures.TOPIC_B) == []
            assert transport.sends == []
            receipts = [json.loads(item["content"]) for item in
                        agents[-1].runtime.actor.transcript.messages if item.get("role") == "tool"]
            assert sum(item.get("reason") == "unknown_intent" for item in receipts) == 4
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_unknown_delivery_keeps_commitment_and_explains_its_source(tmp_path):
    class UnknownTransport(FakeTransport):
        def call(self, method, payload):
            if method == "sendMessage":
                self.calls.append((method, payload))
                return {"ok": True, "result": {}}
            return super().call(method, payload)

    def answer(payload):
        text = payload["observations"][-1]["payload"].get("text")
        if text == "Wait for Alice's result":
            return response(("set_intent", {
                "goal": "Tell Alice the result", "anchor_message_id": 401,
            }))
        if text == "Result is ready":
            return response(
                ("send_message", {"text": "Here is Alice's result.", "anchor_message_id": 401,
                                  "resolves_intent": True}),
                ("wait_for_event", {"purpose": "Alice confirms receipt", "anchor_message_id": 401,
                                    "sender_id": fixtures.ALICE}),
            )
        return response(("finish_silently", {}))

    async def scenario():
        bot, store, clock, transport, payloads, _ = build(
            tmp_path, answer, transport=UnknownTransport(),
        )
        await bot.start()
        try:
            await deliver(bot, clock, message(401, "Wait for Alice's result"))
            await deliver(bot, clock, message(402, "Result is ready", reply_to=401))
            intent_id = store.open_intents(fixtures.TEST_CHAT, fixtures.TOPIC_A)[0]["intent_id"]
            actions = store._query("SELECT * FROM actions")
            assert [(row["status"], row["intent_id"]) for row in actions] == [("unknown", intent_id)]
            for _ in range(3):
                clock.advance(2)
                await bot.autonomous_step(force=True)
            assert len(transport.sends) == 1
            assert store.get_intent(intent_id, chat_id=fixtures.TEST_CHAT,
                                    topic_id=fixtures.TOPIC_A)["status"] == "open"
            assert [row["intent_id"] for row in store.active_waits(
                fixtures.TEST_CHAT, fixtures.TOPIC_A)] == [intent_id]
            event = next(item["payload"] for payload in payloads for item in payload["observations"]
                         if item["type"] == "action_result")
            assert event["status"] == "unknown"
            assert event["intent_id"] == intent_id
            assert event["anchor_message_id"] == 401
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_unattributed_legacy_commitment_can_be_closed_explicitly(tmp_path):
    seen = []

    def answer(payload):
        seen.extend(payload["open_intents"])
        return response(("resolve_intent", {"intent_id": "legacy-goal"}))

    async def scenario():
        bot, store, clock, transport, _, _ = build(tmp_path, answer)
        with store._write() as conn:
            conn.execute(
                "INSERT INTO agent_intents (intent_id, chat_id, topic_id, goal, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, 'open', ?, ?)",
                ("legacy-goal", fixtures.TEST_CHAT, fixtures.TOPIC_A,
                 "Unattributed older commitment", clock(), clock()),
            )
        await bot.start()
        try:
            await deliver(bot, clock, message(501, "What are you waiting for?"))
            assert [(item["intent_id"], item["anchor_message_id"], item["owner_id"])
                    for item in seen] == [("legacy-goal", None, None)]
            assert store.open_intents(fixtures.TEST_CHAT, fixtures.TOPIC_A) == []
            assert transport.sends == []
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_console_commitments_filter_scope_before_limit(tmp_path):
    clock = FakeClock()
    store = Store(tmp_path / "console.sqlite3", clock=clock)
    bot = type("ConsoleBot", (), {"store": store, "chat_kinds": {}})()
    output = io.StringIO()
    console = Console(bot=bot, config=AppConfig(), stream=output)
    try:
        for message_id, topic, label in ((601, fixtures.TOPIC_B, "Bob"),
                                         (602, fixtures.TOPIC_A, "Alice")):
            store.record_inbound_message(
                StoredMessage(fixtures.TEST_CHAT, message_id, topic, fixtures.ALICE,
                              "alice", False, label, clock()),
                update_id=message_id, payload={},
            )
            store.replace_intent(
                intent_id=f"goal-{label}", chat_id=fixtures.TEST_CHAT, topic_id=topic,
                goal=f"Wait for {label}", anchor_message_id=message_id,
            )
            clock.advance(1)
        store.add_wait(
            wait_id="alice-wait", chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A,
            kind="event", purpose="Alice's next result", intent_id="goal-Alice",
        )
        console.watching = (fixtures.TEST_CHAT, fixtures.TOPIC_A)
        console.handle("intents 1")
        rendered = output.getvalue()
        assert "Wait for Alice" in rendered and "Wait for Bob" not in rendered
        assert "message:602" in rendered and f"owner={fixtures.ALICE}" in rendered
        assert "waits=1" in rendered
    finally:
        store.close()
