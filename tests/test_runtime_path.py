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
