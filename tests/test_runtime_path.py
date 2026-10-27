"""Prove the application actually runs on Nervipulsa, not beside it.

The brief is explicit: "不能仅仅做一个导入了 Nervipulsa 名字、实际却使用独立聊天
循环的程序". So these tests do not check that our code was called. They check the
runtime's own objects: its session id, its activation ledger, its transcript,
its event trace, its journal — and that the custom system prompt physically
reached the provider request.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from nervipulsa.config import Settings
from nervipulsa.ports import ModelResponse, ToolCall
from nervipulsa.providers import ScriptedBackend
from nervipulsa.runtime import Runtime

from app import fixtures
from app.agent import NervipulsaAgent
from app.bot import GroupAgent
from app.store import Store
from app.telegram import TelegramClient
from app.turns import TurnManager
from conftest import run


class PromptsSeenBackend(ScriptedBackend):
    """Records every request and answers from the post-marker segment only."""

    def __init__(self) -> None:
        super().__init__(self._respond)

    def _respond(self, request):
        full = request.messages[-1]["content"] if request.messages else ""
        body = full.split("--- new messages ---")[-1]
        if "timezone" in body or "cron" in body:
            return ModelResponse(content="the cron is UTC", model="scripted")
        return ModelResponse(content="can you say more?", model="scripted")


def build(tmp_path: Path, system_prompt: str, backend=None):
    settings = Settings(
        model="scripted", workspace=str(tmp_path / "ws"), max_timeout=30,
        default_timeout=30, context_limit=40_000,
    )
    agent = NervipulsaAgent(
        settings=settings, workspace=tmp_path / "ws", system_prompt=system_prompt,
        backend=backend or PromptsSeenBackend(), timeout=20,
    )
    return agent


def test_a_reply_is_produced_by_the_real_runtime(tmp_path: Path, config, transport,
                                                 clock, system_prompt) -> None:
    backend = PromptsSeenBackend()
    agent = build(tmp_path, system_prompt, backend)
    store = Store(tmp_path / "bot.sqlite3")
    bot = GroupAgent(
        config=config, store=store, client=TelegramClient(transport),
        system_prompt=system_prompt, agent_factory=lambda spec: agent,
        turn_manager=TurnManager(debounce_seconds=2.0, clock=clock),
        bot_user_id=fixtures.ALMA, clock=clock,
    )

    async def body() -> None:
        await bot.start()
        await agent.start()
        runtime = agent.runtime
        assert isinstance(runtime, Runtime)
        session_id = runtime.bus.session_id

        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])

        # 1. The runtime actually activated on our input.
        activations = runtime.actor.activations
        assert len(activations) == 1
        activation = activations[0]
        assert activation.status == "committed"
        assert activation.input_event_ids, "the activation consumed no input event"

        # 2. The runtime's own trace shows the user.message we submitted.
        user_events = [e for e in runtime.trace if e.type == "user.message"]
        assert len(user_events) == 1
        assert user_events[0].session_id == session_id
        assert activation.input_event_ids == (user_events[0].id,)

        # 3. The runtime emitted the assistant message our store recorded.
        assistant_events = [e for e in runtime.trace if e.type == "assistant.message"]
        assert len(assistant_events) == 1
        assert assistant_events[0].payload["activation_id"] == activation.id

        # 4. The transcript the runtime keeps is the one the provider saw.
        transcript_messages = runtime.actor.transcript.messages
        assert any(m.get("role") == "user" and "timezone" in str(m.get("content"))
                   for m in transcript_messages)

        # 5. The request carried our chat rendering and our system prompt.
        request = backend.requests[0]
        assert request.messages[0] == {"role": "system", "content": system_prompt}
        rendered = json.dumps(request.messages, ensure_ascii=False)
        assert "alice:" in rendered
        assert "hey Alma what timezone is the cron in?" in rendered

        # 6. The provider reported the model name back, so a real backend would
        #    have actually been consulted.
        assert request.model == "scripted"
        assert request.activation_id == activation.id

        # 7. The journal is on disk in the runtime's workspace.
        journal = tmp_path / "ws" / ".nervipulsa" / "journal.sqlite"
        assert journal.exists(), "the runtime journal was not created"

        # 8. And the reply reached Telegram exactly once.
        assert len(transport.sends) == 1
        assert "UTC" in transport.sends[0]["text"]

        await agent.stop()
        await bot.stop()

    run(body())
    store.close()


def test_old_recoverable_pause_waits_for_its_new_input(tmp_path: Path, system_prompt) -> None:
    """An input can be accepted before the actor has cleared an old pause."""
    from types import SimpleNamespace

    agent = build(tmp_path, system_prompt)
    agent._timeout = 1.0
    old = SimpleNamespace(id="old", status="paused", error_kind="response_invalid",
                          error_message="empty", context_metrics={"attempts": [{}]})
    actor = SimpleNamespace(paused=old, activations=[old])
    pending = []

    async def release() -> None:
        await asyncio.sleep(0.16)  # More than one polling interval.
        actor.paused = None
        actor.activations.append(SimpleNamespace(
            id="new", status="committed", error_kind=None,
            input_event_ids=("new-input",), context_metrics={},
            response=ModelResponse(content="new answer", model="scripted"),
        ))

    def submit_text(prompt):
        pending.append(asyncio.create_task(release()))
        return SimpleNamespace(accepted=True, event_id="new-input")

    agent._runtime = SimpleNamespace(actor=actor, submit_text=submit_text)

    async def body() -> None:
        result = await agent.ask(chat_id=1, prompt="new message", tag="new")
        await asyncio.gather(*pending)
        assert result.status == "committed"
        assert result.activation_id == "new"
        assert result.text == "new answer"

    run(body())


def test_old_nonrecoverable_pause_is_reported_without_waiting(tmp_path: Path, system_prompt) -> None:
    from types import SimpleNamespace

    async def body() -> None:
        for kind in ("response_unknown", "commit_interrupted"):
            agent = build(tmp_path, system_prompt)
            agent._timeout = 1.0
            old = SimpleNamespace(id="old", status="paused", error_kind=kind,
                                  error_message="blocked", context_metrics={"attempts": [{}]})
            actor = SimpleNamespace(paused=old, activations=[old])
            agent._runtime = SimpleNamespace(
                actor=actor,
                submit_text=lambda prompt: SimpleNamespace(accepted=True, event_id="new-input"),
            )
            result = await asyncio.wait_for(
                agent.ask(chat_id=1, prompt="new message", tag="new"), timeout=0.5
            )
            assert result.status == "paused"
            assert result.detail["error_kind"] == kind
            assert result.detail["not_this_activation"] is True

    run(body())


def test_failed_retry_of_old_pause_does_not_wait_for_full_timeout(tmp_path: Path, system_prompt) -> None:
    from types import SimpleNamespace

    agent = build(tmp_path, system_prompt)
    agent._timeout = 2.0
    old = SimpleNamespace(id="old", status="paused", error_kind="provider_error",
                          error_message="retry failed", context_metrics={"attempts": [{}]})
    actor = SimpleNamespace(paused=old, activations=[old])
    pending = []

    async def retry_fails() -> None:
        await asyncio.sleep(0.16)
        old.context_metrics["attempts"].append({})

    def submit_text(prompt):
        pending.append(asyncio.create_task(retry_fails()))
        return SimpleNamespace(accepted=True, event_id="new-input")

    agent._runtime = SimpleNamespace(actor=actor, submit_text=submit_text)

    async def body() -> None:
        result = await asyncio.wait_for(
            agent.ask(chat_id=1, prompt="new message", tag="new"), timeout=0.8
        )
        await asyncio.gather(*pending)
        assert result.status == "paused"
        assert result.detail["not_this_activation"] is True

    run(body())


def test_no_python_execution_ever_happens(tmp_path: Path, config, transport, clock,
                                          system_prompt) -> None:
    agent = build(tmp_path, system_prompt)
    store = Store(tmp_path / "bot.sqlite3")
    bot = GroupAgent(
        config=config, store=store, client=TelegramClient(transport),
        system_prompt=system_prompt, agent_factory=lambda spec: agent,
        turn_manager=TurnManager(debounce_seconds=2.0, clock=clock),
        bot_user_id=fixtures.ALMA, clock=clock,
    )

    async def body() -> None:
        await bot.start()
        await agent.start()
        runtime = agent.runtime

        for update in [
            fixtures.mention(),
            fixtures.reply_to_bot(),
            fixtures.prompt_injection(),
        ]:
            await bot.step([update])
            clock.advance(3.0)
            await bot.step([])

        assert [e for e in runtime.trace if e.type == "python.requested"] == []
        assert runtime.host.current_execution_id is None
        assert runtime.host.queue_size == 0
        assert runtime.actor.tools == []
        # worker_epoch may be >= 1 because start() spawns a worker; what matters
        # is that nothing was ever submitted to it. Recorded in RUNTIME_GAPS.md.
        assert all(e.type != "python.finished" for e in runtime.trace)

        await agent.stop()
        await bot.stop()

    run(body())
    store.close()


def test_model_attempting_a_tool_call_is_rejected_by_the_runtime(tmp_path: Path,
                                                                 system_prompt) -> None:
    """A hostile or confused model cannot reach Python through this application."""

    class ToolCallingBackend(ScriptedBackend):
        def __init__(self) -> None:
            super().__init__(self._respond)

        def _respond(self, request):
            return ModelResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="python_exec",
                                     arguments={"code": "open('pwned','w').write('x')"})],
            )

    agent = build(tmp_path, system_prompt, ToolCallingBackend())

    async def body() -> None:
        await agent.start()
        result = await agent.ask(chat_id=1, prompt="hello", tag="t")
        runtime = agent.runtime
        # The runtime rejected the call; the application got no usable text.
        assert result.text in (None, "")
        assert [e for e in runtime.trace if e.type == "python.requested"] == []
        feedback = [e for e in runtime.trace if e.type == "agent.feedback"]
        assert feedback, "no rejection feedback was emitted"
        reasons = [r.get("reason") for r in feedback[0].payload.get("rejections", [])]
        assert "unknown_tool" in reasons, reasons
        assert not (tmp_path / "ws" / "pwned").exists()
        await agent.stop()

    run(body())


def test_each_chat_gets_its_own_runtime(tmp_path: Path, config, clock, system_prompt) -> None:
    """Two chats must not share a transcript, a session, or a model view."""
    from app.config import AppConfig

    config2 = AppConfig(**{**config.__dict__, "allowed_chat_ids": (
        fixtures.TEST_CHAT, fixtures.OTHER_CHAT)})
    created: dict[int, NervipulsaAgent] = {}

    def factory(spec):
        agent = NervipulsaAgent(
            settings=Settings(model="scripted", workspace=str(spec.workspace),
                              max_timeout=30, default_timeout=30),
            workspace=spec.workspace, system_prompt=spec.system_prompt,
            backend=PromptsSeenBackend(), timeout=20,
        )
        created[spec.chat_id] = agent
        return agent

    store = Store(tmp_path / "bot.sqlite3")
    bot = GroupAgent(
        config=config2, store=store, client=TelegramClient(transport=None or _NullTransport()),
        system_prompt=system_prompt, agent_factory=factory,
        turn_manager=TurnManager(debounce_seconds=2.0, clock=clock),
        bot_user_id=fixtures.ALMA, clock=clock,
    )

    async def body() -> None:
        await bot.start()
        for chat in (fixtures.TEST_CHAT, fixtures.OTHER_CHAT):
            await bot.step([fixtures.message(
                update_id=chat % 1000, message_id=1, chat_id=chat, sender_id=fixtures.ALICE,
                username="alice", text="Alma are you there?")])
        clock.advance(3.0)
        await bot.step([])
        assert set(created) == {fixtures.TEST_CHAT, fixtures.OTHER_CHAT}
        first, second = created[fixtures.TEST_CHAT], created[fixtures.OTHER_CHAT]
        assert first is not second
        assert first.runtime.bus.session_id != second.runtime.bus.session_id
        assert first.runtime.actor.transcript is not second.runtime.actor.transcript
        await bot.stop()

    run(body())
    store.close()


class _NullTransport:
    def call(self, method, payload):
        return {"ok": True, "result": {"message_id": 1}}


def test_two_forum_topics_get_separate_runtimes(tmp_path: Path, config, clock,
                                                system_prompt) -> None:
    """A live simulation caught this: keying agents by chat alone let topic B
    answer from topic A's transcript. One runtime per (chat, topic)."""
    created: dict[str, NervipulsaAgent] = {}

    def factory(spec):
        key = str(spec.workspace)
        agent = NervipulsaAgent(
            settings=Settings(model="scripted", workspace=str(spec.workspace),
                              max_timeout=30, default_timeout=30),
            workspace=spec.workspace, system_prompt=spec.system_prompt,
            backend=PromptsSeenBackend(), timeout=20,
        )
        created[key] = agent
        return agent

    store = Store(tmp_path / "bot.sqlite3")
    bot = GroupAgent(
        config=config, store=store, client=TelegramClient(_NullTransport()),
        system_prompt=system_prompt, agent_factory=factory,
        turn_manager=TurnManager(debounce_seconds=2.0, clock=clock),
        bot_user_id=fixtures.ALMA, clock=clock,
    )

    async def body() -> None:
        await bot.start()
        await bot.step([
            fixtures.message(update_id=1, message_id=1, sender_id=fixtures.ALICE,
                             username="alice", text="Alma what is the deploy timeout?",
                             topic_id=fixtures.TOPIC_A),
            fixtures.message(update_id=2, message_id=2, sender_id=fixtures.CAROL,
                             username="carol", text="Alma what did we just say?",
                             topic_id=fixtures.TOPIC_B),
        ])
        clock.advance(3.0)
        await bot.step([])

        assert len(created) == 2, f"expected two runtimes, got {list(created)}"
        first, second = created.values()
        assert first is not second
        assert first.runtime.bus.session_id != second.runtime.bus.session_id
        assert first.runtime.actor.transcript is not second.runtime.actor.transcript
        # Neither transcript contains the other topic's prompt.
        for agent, forbidden in ((first, "what did we just say"),
                                 (second, "deploy timeout")):
            rendered = json.dumps(agent.runtime.actor.transcript.messages,
                                  ensure_ascii=False)
            assert forbidden not in rendered, f"cross-topic leak: {forbidden!r}"
        await bot.stop()

    run(body())
    store.close()


def test_same_topic_reuses_its_runtime(tmp_path: Path, config, clock,
                                      system_prompt) -> None:
    """Reuse must still happen, or every message would start a new worker."""
    created: list[str] = []

    def factory(spec):
        created.append(str(spec.workspace))
        return NervipulsaAgent(
            settings=Settings(model="scripted", workspace=str(spec.workspace),
                              max_timeout=30, default_timeout=30),
            workspace=spec.workspace, system_prompt=spec.system_prompt,
            backend=PromptsSeenBackend(), timeout=20,
        )

    store = Store(tmp_path / "bot.sqlite3")
    bot = GroupAgent(
        config=config, store=store, client=TelegramClient(_NullTransport()),
        system_prompt=system_prompt, agent_factory=factory,
        turn_manager=TurnManager(debounce_seconds=2.0, clock=clock),
        bot_user_id=fixtures.ALMA, clock=clock,
    )

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        await bot.step([fixtures.reply_to_bot()])
        clock.advance(3.0)
        await bot.step([])
        assert len(created) == 1, f"a second runtime was created: {created}"
        await bot.stop()

    run(body())
    store.close()


def test_transcript_grows_only_with_what_the_runtime_was_given(tmp_path: Path,
                                                               config, transport, clock,
                                                               system_prompt) -> None:
    """Silence must not add anything to the runtime's context."""
    agent = build(tmp_path, system_prompt)
    store = Store(tmp_path / "bot.sqlite3")
    bot = GroupAgent(
        config=config, store=store, client=TelegramClient(transport),
        system_prompt=system_prompt, agent_factory=lambda spec: agent,
        turn_manager=TurnManager(debounce_seconds=2.0, clock=clock),
        bot_user_id=fixtures.ALMA, clock=clock,
    )

    async def body() -> None:
        await bot.start()
        await agent.start()
        runtime = agent.runtime
        await bot.step(fixtures.plain_chat())
        clock.advance(3.0)
        await bot.step([])
        assert runtime.actor.transcript.messages == []
        assert runtime.actor.activations == []
        assert [e for e in runtime.trace if e.type == "user.message"] == []
        await agent.stop()
        await bot.stop()

    run(body())
    store.close()


def test_agent_timeout_is_reported_not_hidden(tmp_path: Path, system_prompt) -> None:
    """A model that never answers must produce a timeout result, not a reply."""

    class HangingBackend(ScriptedBackend):
        def __init__(self) -> None:
            super().__init__(self._respond)

        async def _respond(self, request):
            await asyncio.sleep(30)
            return ModelResponse(content="too late", model="scripted")

    agent = build(tmp_path, system_prompt, HangingBackend())

    async def body() -> None:
        agent._timeout = 0.5
        await agent.start()
        result = await agent.ask(chat_id=1, prompt="hello", tag="t")
        assert result.status == "timeout"
        assert result.text is None
        assert result.detail["timeout_seconds"] == 0.5
        await agent.stop()

    run(body())


def test_provider_failure_is_surfaced_as_an_error_result(tmp_path: Path,
                                                         system_prompt) -> None:
    from nervipulsa.ports import ProviderFailure

    class FailingBackend(ScriptedBackend):
        def __init__(self) -> None:
            super().__init__(self._respond)

        def _respond(self, request):
            raise ProviderFailure("response_unknown", "provider response unknown: reset")

    agent = build(tmp_path, system_prompt, FailingBackend())

    async def body() -> None:
        await agent.start()
        result = await agent.ask(chat_id=1, prompt="hello", tag="t")
        assert result.status == "error"
        assert result.text is None
        assert result.detail["error_kind"] == "response_unknown"
        await agent.stop()

    run(body())


def test_real_runtime_recovers_from_invalid_reply_on_new_input(tmp_path: Path, system_prompt) -> None:
    calls = []

    def respond(request):
        calls.append(request)
        return ModelResponse(content="" if len(calls) == 1 else "new answer", model="scripted")

    agent = build(tmp_path, system_prompt, ScriptedBackend(respond))

    async def body() -> None:
        await agent.start()
        try:
            first = await agent.ask(chat_id=1, prompt="old message", tag="old")
            second = await agent.ask(chat_id=1, prompt="new message", tag="new")
            assert first.status == "error"
            assert first.detail["error_kind"] == "response_invalid"
            assert second.status == "committed"
            assert second.text == "new answer"
            assert second.activation_id != first.activation_id
            assert len(calls) == 2
            assert any(message.get("content") == "new message" for message in calls[1].messages)
        finally:
            await agent.stop()

    run(body())
