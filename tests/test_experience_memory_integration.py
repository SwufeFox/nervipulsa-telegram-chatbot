"""Sourced memory changes what a scoped, real Runtime can recall and do."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from nervipulsa.config import Settings
from nervipulsa.ports import ModelResponse, ToolCall
from nervipulsa.providers import ScriptedBackend

from app import fixtures
from app.agent import NervipulsaAgent
from app.autonomy import _ActionHandler
from app.bot import GroupAgent
from app.config import AppConfig, load_default_profile
from app.memory import MAX_MEMORY_READ_ROUNDS, MAX_MEMORY_READS_PER_WORKFLOW
from app.store import Store, StoredMessage
from app.telegram import TelegramClient
from conftest import FakeClock, FakeTransport, run


def tools(*calls):
    return ModelResponse(tool_calls=[
        ToolCall(id=f"call-{uuid.uuid4().hex}", name=name, arguments=arguments)
        for name, arguments in calls
    ])


def finish():
    return tools(("finish_silently", {}))


def source(payload):
    return next(item for item in reversed(payload["observations"])
                if item["type"] in {"message", "message_edit"})


def text_of(payload):
    return source(payload)["payload"]["text"]


class Rig:
    def __init__(self, root, answer, *, clock=None, transport=None):
        self.clock = clock if clock is not None else FakeClock()
        self.transport = transport if transport is not None else FakeTransport()
        self.store = Store(root / "app.sqlite3", clock=self.clock)
        self.payloads = []
        self.agents = {}
        prompt = load_default_profile()

        def complete(request):
            assert request.purpose == "activation"
            payload = json.loads(request.messages[-1]["content"])["payload"]
            if "observations" not in payload:
                return finish()
            self.payloads.append(payload)
            return answer(payload)

        self.backend = ScriptedBackend(complete)
        config = AppConfig(
            bot_username="alma_bot", allowed_chat_ids=(fixtures.TEST_CHAT, fixtures.OTHER_CHAT),
            workspace=str(root), model="scripted", idle_seconds=0, max_batch_seconds=0,
            min_action_interval_seconds=0, max_activations_per_hour=100,
            max_actions_per_hour=100,
        )

        def factory(spec):
            agent = NervipulsaAgent(
                settings=Settings(model="scripted", context_limit=200_000),
                workspace=spec.workspace, system_prompt=prompt, backend=self.backend,
                timeout=10, tool_catalog=spec.tool_catalog,
                response_commit_hook=spec.response_commit_hook,
                session_id=spec.session_id, restore=spec.restore,
            )
            self.agents[spec.session_id] = agent
            return agent

        self.bot = GroupAgent(
            config=config, store=self.store, client=TelegramClient(self.transport),
            system_prompt=prompt, agent_factory=factory,
            bot_user_id=fixtures.ALMA, clock=self.clock,
        )

    async def start(self):
        await self.bot.start()

    async def close(self):
        await self.bot.stop()
        self.store.close()

    async def deliver(self, identity, text, *, sender=fixtures.ALICE,
                      topic=fixtures.TOPIC_A, chat=fixtures.TEST_CHAT,
                      reply_to=None, edit_message_id=None, activate=True):
        update = fixtures.message(
            update_id=identity, message_id=edit_message_id or identity,
            chat_id=chat, topic_id=topic, sender_id=sender,
            username="alice" if sender == fixtures.ALICE else "bob",
            text=text, reply_to_message_id=reply_to, date=int(self.clock()),
        )
        if edit_message_id is not None:
            update["edited_message"] = update.pop("message")
        await self.bot.step([update], run=False)
        self.clock.advance(1)
        if activate:
            return await self.bot.autonomous_step(force=True)
        return []

    def memories(self, **filters):
        return self.store.recall_memories(fixtures.TEST_CHAT, fixtures.TOPIC_A, **filters)


def test_person_memory_outlives_finished_episode_and_restart(tmp_path):
    def answer(payload):
        if text_of(payload) == "I prefer one short sentence":
            return tools(
                ("remember", {
                    "summary": "Alice prefers one short sentence", "kind": "person",
                    "source_sequences": [source(payload)["sequence"]],
                    "person_ids": [fixtures.ALICE], "certainty": "observed",
                    "tags": ["reply style"],
                }),
                ("finish_silently", {}),
            )
        remembered = next((item for item in payload["memories"]
                           if fixtures.ALICE in item["person_ids"]), None)
        return tools(("send_message", {
            "text": "One sentence, as you prefer." if remembered else "What style do you prefer?",
            **({"memory_id": remembered["memory_id"], "memory_revision": remembered["revision"]}
               if remembered else {}),
        }))

    async def scenario():
        first = Rig(tmp_path, answer)
        await first.start()
        try:
            await first.deliver(101, "I prefer one short sentence")
            memory_id = first.memories()[0]["memory_id"]
            assert first.store.current_episode(fixtures.TEST_CHAT, fixtures.TOPIC_A) is None
            assert first.transport.sends == []
        finally:
            await first.close()
        second = Rig(tmp_path, answer, clock=first.clock, transport=first.transport)
        await second.start()
        try:
            await second.deliver(102, "Can you explain this?")
            assert [item["text"] for item in second.transport.sends] == ["One sentence, as you prefer."]
            assert second.memories()[0]["memory_id"] == memory_id
            sent = second.store._query("SELECT * FROM actions WHERE status='sent'")[0]
            assert (sent["memory_id"], sent["memory_revision"]) == (memory_id, 1)
        finally:
            await second.close()

    run(scenario())


def test_explicit_recall_and_history_continue_in_one_workflow(tmp_path):
    saved = {}

    def answer(payload):
        if payload["kind"] == "memory_tool_results":
            receipt = payload["memory_tool_results"][-1]
            if receipt["tool"] == "recall_memories":
                remembered = receipt["memories"][0]
                saved.update(memory_id=remembered["memory_id"], revision=remembered["revision"])
                return tools(("read_history", {"message_id": 201, "limit": 1}))
            original = receipt["history"][0]["payload"]["text"]
            return tools(("send_message", {
                "text": f"The earlier record says: {original}",
                "memory_id": saved["memory_id"], "memory_revision": saved["revision"],
            }))
        if text_of(payload) == "Restarting the cache fixed the outage":
            return tools(
                ("remember", {"summary": "The cache outage was fixed by restarting it",
                              "kind": "experience", "source_sequences": [source(payload)["sequence"]],
                              "person_ids": [fixtures.ALICE], "tags": ["cache outage"]}),
                ("finish_silently", {}),
            )
        return tools(("recall_memories", {"source_message_id": 201}))

    async def scenario():
        rig = Rig(tmp_path, answer)
        await rig.start()
        try:
            await rig.deliver(201, "Restarting the cache fixed the outage")
            calls = rig.bot.autonomy.model_calls
            result = (await rig.deliver(202, "How did that outage end?", sender=fixtures.BOB))[0]
            assert [item["text"] for item in rig.transport.sends] == [
                "The earlier record says: Restarting the cache fixed the outage",
            ]
            assert rig.bot.autonomy.model_calls - calls == 3
            assert result.detail["memory_reads"] == 2
            assert len(result.detail["activations"]) == 3
            assert result.state == "completed"
            assert rig.store.observation_cursor(fixtures.TEST_CHAT, fixtures.TOPIC_A) == 2
            assert len(rig.store._query("SELECT * FROM workflows")) == 2
        finally:
            await rig.close()

    run(scenario())


def test_edit_correction_preserves_history_and_cancels_old_pending_reply(tmp_path):
    def answer(payload):
        if payload["kind"] == "memory_tool_results":
            return finish()
        if source(payload)["type"] == "message_edit":
            old = payload["memories"][0]
            assert old["needs_review"] is True
            return tools(("remember", {
                "memory_id": old["memory_id"], "expected_revision": old["revision"],
                "summary": "Alice uses Asia/Shanghai", "kind": "person",
                "source_sequences": [source(payload)["sequence"]],
                "person_ids": [fixtures.ALICE], "certainty": "observed",
            }))
        if text_of(payload) == "I use UTC":
            return tools(
                ("remember", {"summary": "Alice uses UTC", "kind": "person",
                              "source_sequences": [source(payload)["sequence"]],
                              "person_ids": [fixtures.ALICE], "certainty": "observed"}),
                ("finish_silently", {}),
            )
        memory = payload["memories"][0]
        return tools(("send_message", {
            "text": memory["summary"], "memory_id": memory["memory_id"],
            "memory_revision": memory["revision"],
        }))

    async def scenario():
        rig = Rig(tmp_path, answer)
        await rig.start()
        try:
            await rig.deliver(301, "I use UTC")
            original = rig.memories()[0]
            rig.store.prepare_action(
                action_id="old-timezone", workflow_id=None, chat_id=fixtures.TEST_CHAT,
                topic_id=fixtures.TOPIC_A, text="Use UTC", observed_through=1,
                memory_id=original["memory_id"], memory_revision=1,
            )
            await rig.deliver(302, "Actually I use Asia/Shanghai", edit_message_id=301)
            current = rig.memories()[0]
            assert current["memory_id"] == original["memory_id"]
            assert (current["revision"], current["needs_review"]) == (2, False)
            assert rig.store.get_action("old-timezone")["status"] == "cancelled"
            history = rig.store.memory_history(
                original["memory_id"], chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A,
            )
            assert [item["summary"] for item in history] == ["Alice uses UTC", "Alice uses Asia/Shanghai"]
            assert history[0]["sources"][0]["text"] == "I use UTC"
            await rig.deliver(303, "Which timezone am I using?")
            assert [item["text"] for item in rig.transport.sends] == ["Alice uses Asia/Shanghai"]
        finally:
            await rig.close()

    run(scenario())


@pytest.mark.parametrize("retire", [None, "forget", "expire"])
def test_own_interest_revisit_survives_chat_and_restart_or_is_retired(tmp_path, retire):
    clock = FakeClock()
    due = datetime.fromtimestamp(clock() + 60, timezone.utc).isoformat()
    expiry = datetime.fromtimestamp(clock() + 30, timezone.utc).isoformat()
    remembered = {}

    def answer(payload):
        if payload["kind"] == "memory_tool_results":
            receipt = payload["memory_tool_results"][-1]
            if receipt["tool"] == "remember":
                remembered.update(receipt)
                return tools(("wait_until", {
                    "at": due, "purpose": "Revisit the cache behavior out of curiosity",
                    "memory_id": receipt["memory_id"],
                }))
            return finish()
        if any(item["type"] == "timer" for item in payload["observations"]):
            memory = next(item for item in payload["memories"]
                          if item["memory_id"] == remembered["memory_id"])
            return tools(("send_message", {
                "text": "I am still curious whether that cache behavior recurred.",
                "memory_id": memory["memory_id"], "memory_revision": memory["revision"],
            }))
        if text_of(payload) == "The cache behavior was strange":
            return tools(("remember", {
                "summary": "I want to understand whether the cache behavior recurs",
                "kind": "interest", "source_sequences": [source(payload)["sequence"]],
                **({"expires_at": expiry} if retire == "expire" else {}),
            }))
        if text_of(payload) == "Drop that curiosity":
            return tools(("forget_memory", {
                "memory_id": remembered["memory_id"], "expected_revision": remembered["revision"],
            }))
        return finish()

    async def scenario():
        first = Rig(tmp_path, answer, clock=clock)
        await first.start()
        try:
            await first.deliver(401, "The cache behavior was strange")
            assert first.store.active_waits(fixtures.TEST_CHAT, fixtures.TOPIC_A)[0]["memory_id"] == remembered["memory_id"]
            await first.deliver(402, "Another conversation", sender=fixtures.BOB)
            assert first.store.active_waits(fixtures.TEST_CHAT, fixtures.TOPIC_A)[0]["memory_id"] == remembered["memory_id"]
            if retire == "forget":
                await first.deliver(403, "Drop that curiosity")
        finally:
            await first.close()
        second = Rig(tmp_path, answer, clock=clock, transport=first.transport)
        await second.start()
        try:
            clock.advance(70)
            await second.bot.autonomous_step(force=False)
            if retire is None:
                assert [item["text"] for item in second.transport.sends] == [
                    "I am still curious whether that cache behavior recurred.",
                ]
                assert second.memories()[0]["memory_id"] == remembered["memory_id"]
            else:
                assert second.transport.sends == []
                assert second.memories() == []
            assert second.store.active_waits(fixtures.TEST_CHAT, fixtures.TOPIC_A) == []
        finally:
            await second.close()

    run(scenario())


def test_repeated_reads_are_bounded_and_not_counted_as_silence(tmp_path):
    def answer(payload):
        return tools(("recall_memories", {}), ("read_history", {}))

    async def scenario():
        rig = Rig(tmp_path, answer)
        await rig.start()
        try:
            result = (await rig.deliver(501, "Read until you decide"))[0]
            assert result.outcome == "undecided"
            assert rig.bot.autonomy.model_calls == 1 + MAX_MEMORY_READ_ROUNDS
            assert result.detail["memory_reads"] == MAX_MEMORY_READS_PER_WORKFLOW
            assert rig.bot.autonomy.silent_workflows == 0
            assert rig.transport.sends == []
            assert rig.store.observation_cursor(fixtures.TEST_CHAT, fixtures.TOPIC_A) == 1
            assert rig.store._query("SELECT reason FROM observation_skips")
        finally:
            await rig.close()

    run(scenario())


def test_foreign_topic_recall_and_history_do_not_leak_memory(tmp_path):
    references = {}
    secret = "Alice's private topic detail"

    def answer(payload):
        if payload["topic_id"] == fixtures.TOPIC_A:
            return tools(
                ("remember", {"summary": secret, "kind": "person", "person_ids": [fixtures.ALICE],
                              "source_sequences": [source(payload)["sequence"]]}),
                ("finish_silently", {}),
            )
        if payload["kind"] == "memory_tool_results":
            assert all(not item.get("memories") and not item.get("history")
                       for item in payload["memory_tool_results"])
            return finish()
        return tools(("recall_memories", {"memory_id": references["memory_id"]}),
                     ("read_history", {"message_id": 601}))

    async def scenario():
        rig = Rig(tmp_path, answer)
        await rig.start()
        try:
            await rig.deliver(601, secret)
            references["memory_id"] = rig.memories()[0]["memory_id"]
            await rig.deliver(602, "Look in another topic", sender=fixtures.BOB, topic=fixtures.TOPIC_B)
            session_id = rig.bot._session_id_for(fixtures.TEST_CHAT, fixtures.TOPIC_B)
            transcript = rig.agents[session_id].runtime.actor.transcript.messages
            assert secret not in json.dumps(transcript, ensure_ascii=False)
            assert rig.transport.sends == []
        finally:
            await rig.close()

    run(scenario())


def test_future_input_cannot_be_saved_or_read_from_frozen_workflow(tmp_path):
    rig = None
    injected = False

    def answer(payload):
        nonlocal injected
        if payload["kind"] == "memory_tool_results":
            history = next(item for item in payload["memory_tool_results"] if item["tool"] == "read_history")
            assert history["history"] == []
            return finish()
        if not injected:
            injected = True
            rig.store.record_inbound_message(
                StoredMessage(fixtures.TEST_CHAT, 702, fixtures.TOPIC_A, fixtures.ALICE,
                              "alice", False, "Future evidence", rig.clock()),
                update_id=702, payload={},
            )
        return tools(
            ("remember", {"summary": "Unseen future claim", "kind": "experience", "source_sequences": [2]}),
            ("read_history", {"message_id": 702}),
        )

    async def scenario():
        nonlocal rig
        rig = Rig(tmp_path, answer)
        await rig.start()
        try:
            await rig.deliver(701, "Only this input was presented")
            assert rig.memories() == []
            receipts = [json.loads(row["receipt_json"]) for row in rig.store._query(
                "SELECT receipt_json FROM autonomous_calls WHERE name='remember' AND receipt_json IS NOT NULL")]
            assert receipts[0]["reason"] == "invalid_source"
            assert rig.store.observation_cursor(fixtures.TEST_CHAT, fixtures.TOPIC_A) == 1
            assert rig.transport.sends == []
        finally:
            await rig.close()

    run(scenario())


def test_an_edit_received_during_inference_keeps_old_evidence_under_review(tmp_path):
    rig = None
    injected = False

    def answer(payload):
        nonlocal injected
        if payload["kind"] == "memory_tool_results":
            assert payload["memory_tool_results"][0]["needs_review"] is True
            return finish()
        if not injected:
            injected = True
            rig.store.record_inbound_message(
                StoredMessage(fixtures.TEST_CHAT, 801, fixtures.TOPIC_A, fixtures.ALICE,
                              "alice", False, "Actually Shanghai", rig.clock()),
                update_id=802, payload={},
            )
        return tools(("remember", {
            "summary": "Alice uses UTC", "kind": "person", "person_ids": [fixtures.ALICE],
            "source_sequences": [source(payload)["sequence"]], "certainty": "observed",
        }))

    async def scenario():
        nonlocal rig
        rig = Rig(tmp_path, answer)
        await rig.start()
        try:
            await rig.deliver(801, "I use UTC")
            memory = rig.memories()[0]
            assert memory["needs_review"] is True
            with pytest.raises(ValueError, match="memory_needs_review"):
                rig.store.prepare_action(
                    action_id="stale-after-edit", workflow_id=None, chat_id=fixtures.TEST_CHAT,
                    topic_id=fixtures.TOPIC_A, text="Use UTC", observed_through=1,
                    memory_id=memory["memory_id"], memory_revision=memory["revision"],
                )
            assert rig.transport.sends == []
        finally:
            await rig.close()

    run(scenario())


def test_committed_memory_mutation_replay_does_not_create_another_memory(tmp_path):
    def answer(payload):
        return tools(
            ("remember", {"summary": "A shared cache incident", "kind": "experience",
                          "source_sequences": [source(payload)["sequence"]]}),
            ("finish_silently", {}),
        )

    async def scenario():
        rig = Rig(tmp_path, answer)
        await rig.start()
        try:
            await rig.deliver(901, "We fixed the cache together")
            original = rig.memories()[0]
            row = rig.store._query("SELECT * FROM autonomous_calls WHERE name='remember'")[0]
            handler = _ActionHandler(
                rig.bot.autonomy, chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A,
                workflow_id="replayed-call", observed_through=1,
            )
            receipt, state = await handler(
                "remember", json.loads(row["arguments_json"]), None,
                SimpleNamespace(id=row["activation_id"]), SimpleNamespace(id=row["call_id"]),
            )
            assert state == "accepted" and receipt["memory_id"] == original["memory_id"]
            assert [item["memory_id"] for item in rig.memories()] == [original["memory_id"]]
            assert [item["revision"] for item in rig.store.memory_history(
                original["memory_id"], chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A)] == [1]
        finally:
            await rig.close()

    run(scenario())


def test_live_send_feedback_updates_later_behavior_without_an_acknowledgement(tmp_path, monkeypatch):
    def answer(payload):
        feedback = next((item for item in payload["observations"] if item["type"] == "feedback"), None)
        if feedback:
            assert feedback["payload"]["reply_text"] == "Use the scheduler timezone setting."
            return tools(
                ("remember", {"summary": "Ask which scheduler before suggesting timezone configuration",
                              "kind": "preference", "certainty": "inferred",
                              "source_sequences": [feedback["sequence"]]}),
                ("finish_silently", {}),
            )
        if text_of(payload) == "First timezone question":
            return tools(("send_message", {"text": "Use the scheduler timezone setting."}))
        preference = next((item for item in payload["memories"] if item["kind"] == "preference"), None)
        return tools(("send_message", {
            "text": "Which scheduler are you using?" if preference else "Set UTC directly.",
        }))

    async def scenario():
        rig = Rig(tmp_path, answer)
        await rig.start()
        try:
            await rig.deliver(1001, "First timezone question")
            sent = rig.store._query("SELECT * FROM actions WHERE status='sent'")[0]
            reaction = {
                "update_id": 1002,
                "message_reaction": {
                    "chat": {"id": fixtures.TEST_CHAT, "type": "supergroup"},
                    "message_id": sent["telegram_message_id"], "user": {"id": 777777777},
                    "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "\U0001f44e"}],
                },
            }
            original_cursor = rig.store.set_update_cursor

            def fail_cursor(_offset):
                raise RuntimeError("injected commit-boundary failure")

            monkeypatch.setattr(rig.store, "set_update_cursor", fail_cursor)
            rig.transport.updates = [[reaction]]
            with pytest.raises(RuntimeError):
                await rig.bot.poll_once(offset=1002, background=True)
            assert not rig.store.has_update(1002)
            calls = rig.bot.autonomy.model_calls
            await rig.bot.autonomous_step(force=True)
            assert rig.bot.autonomy.model_calls == calls
            monkeypatch.setattr(rig.store, "set_update_cursor", original_cursor)
            rig.transport.updates = [[reaction]]
            offset, tick = await rig.bot.poll_once(offset=1002, background=True)
            assert offset == 1003 and tick.feedback_updates == 1
            await rig.bot.autonomous_step(force=True)
            preference = rig.memories(kind="preference")[0]
            assert preference["person_ids"] == []
            assert preference["sources"][0]["sender_id"] is None
            assert len(rig.transport.sends) == 1
            report = rig.store.feedback_report(fixtures.TEST_CHAT)
            assert report["down"] == 1 and report["items"][0]["action_id"] == sent["action_id"]
            await rig.deliver(1003, "Another timezone question")
            assert [item["text"] for item in rig.transport.sends] == [
                "Use the scheduler timezone setting.", "Which scheduler are you using?",
            ]
        finally:
            await rig.close()

    run(scenario())
