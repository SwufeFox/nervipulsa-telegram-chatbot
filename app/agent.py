"""The adapter between the application and Nervipulsa.

In production, ``GroupAgent._agent_for`` creates one ``NervipulsaAgent`` and
Runtime per ``(chat_id, topic_id)``. That keeps the model transcript and tool
binding inside a single chat/topic scope.

The adapter accepts an injected tool catalog. Autonomous scopes receive the
closed seven-tool action catalog from ``app.autonomous_tools``; callers without
a catalog get ``EmptyToolCatalog`` and cannot act. Every Runtime has
``python_host=False``, so Python execution tools and interpreters are never
available.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from nervipulsa.config import Settings
from nervipulsa.ports import ModelBackend, ModelResponse, ProviderFailure
from nervipulsa.providers import OpenAICompatibleBackend
from nervipulsa.runtime import Runtime

log = logging.getLogger(__name__)


class EmptyToolCatalog:
    """Fallback catalog that exposes no tools when a caller has no action catalog."""

    def schemas(self, *, workspace: str | None = None) -> list[dict[str, Any]]:
        return []

    def get(self, name: str) -> None:
        return None


@dataclass
class AgentResult:
    """What one activation produced."""

    activation_id: str
    text: str | None
    status: str
    detail: dict[str, Any] = field(default_factory=dict)


class AgentAdapter(Protocol):
    """The narrow surface the application depends on. Tests substitute this."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def ask(self, *, chat_id: int, prompt: str, tag: str) -> AgentResult: ...


class ScriptedAgent:
    """Deterministic stand-in used by the offline experiment and by tests.

    This is NOT the app's chat loop: it implements the same protocol the real
    adapter does, so the caller code path is identical. Milestone A uses it to
    drive real `Runtime` behaviour through `NervipulsaAgent`'s sibling; the
    `tests/test_runtime_path.py` suite additionally proves the real runtime is
    exercised by inspecting the runtime's own trace.
    """

    def __init__(self, responder: Callable[[str], str | None]) -> None:
        self._responder = responder
        self.calls: list[str] = []
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def ask(self, *, chat_id: int, prompt: str, tag: str) -> AgentResult:
        if not self._started:
            raise RuntimeError("agent not started")
        self.calls.append(prompt)
        text = self._responder(prompt)
        return AgentResult(
            activation_id=f"scripted_{len(self.calls)}",
            text=text,
            status="committed",
            detail={"tag": tag, "scripted": True},
        )


class NervipulsaAgent:
    """Wraps one `Runtime` and turns a prompt into `AgentResult`.

    Replies come from the activation correlated with the submitted input.
    Core owns waiting, persistence, and session ownership; the application
    binds only the current workflow's action handler.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        workspace: Path,
        system_prompt: str,
        backend: ModelBackend | None = None,
        timeout: float = 60.0,
        tool_catalog: Any | None = None,
        response_commit_hook: Any | None = None,
        session_id: str | None = None,
        restore: bool = False,
        vision_resolver: Any | None = None,
        vision_observation_check: Any | None = None,
    ) -> None:
        if not system_prompt.strip():
            raise ValueError("a non-empty system prompt is required")
        self._settings = settings
        self._workspace = workspace
        self._system_prompt = system_prompt
        self._backend = backend or OpenAICompatibleBackend(
            provider=settings.provider,
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
            timeout=timeout,
            adapter=settings.adapter,
        )
        self._timeout = timeout
        self._tool_catalog = tool_catalog
        self._response_commit_hook = response_commit_hook
        # A stable session id is what makes the model view survive a restart:
        # the journal is keyed by it, and a random id per process would make
        # every conversation start from nothing. Default stays random for
        # callers that do not want persistence.
        self._session_id = session_id
        self._restore = restore
        self.restore_report: dict[str, Any] | None = None
        self._runtime: Runtime | None = None
        if vision_resolver is not None and vision_observation_check is not None:
            from .vision import VisionBackend

            def trusted(index: int, message: dict, envelope: dict) -> bool:
                if self._runtime is None or index < 1:
                    return False
                transcript = self._runtime.actor.transcript
                i = index - 1 - (self._runtime.actor.projector.status_message() is not None)
                return (0 <= i < len(transcript.messages) and i < len(transcript.event_ids)
                        and i < len(transcript.event_types)
                        and transcript.messages[i] == message
                        and transcript.event_types[i] == "runtime.observation"
                        and transcript.event_ids[i] is not None
                        and transcript.event_ids[i] == envelope.get("event_id")
                        and vision_observation_check(envelope))

            self._backend = VisionBackend(self._backend, vision_resolver, trusted)
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()

    @property
    def runtime(self) -> Runtime | None:
        return self._runtime

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._runtime is not None:
                return
            from .autonomous_tools import CATALOG_TOOL_NAMES

            allowed = set()
            if self._tool_catalog is not None:
                allowed = set(self._tool_catalog.names)
                if allowed != set(CATALOG_TOOL_NAMES):
                    raise RuntimeError(f"tool catalog exposes an unexpected set: {sorted(allowed)}")

            runtime = Runtime(
                self._settings,
                self._backend,
                self._workspace,
                tool_catalog=self._tool_catalog if self._tool_catalog is not None else EmptyToolCatalog(),
                response_commit_hook=self._response_commit_hook,
                system_prompt=self._system_prompt,
                python_host=False,
                session_id=self._session_id,
            )
            exposed = [schema.get("function", {}).get("name") for schema in runtime.actor.tools]
            if set(exposed) != allowed or len(exposed) != len(allowed):
                raise RuntimeError(
                    f"runtime exposed tools do not match the action catalog: "
                    f"expected={sorted(allowed)}, exposed={exposed}"
                )

            # Core leases the session and restores its committed model view
            # off the event loop before it starts accepting observations.
            self.restore_report = await runtime.start(restore_transcript=self._restore)
            self._runtime = runtime
            if self.restore_report and self.restore_report.get("restored"):
                log.info(
                    "restored model view for session %s: %s messages, %s ids",
                    self._session_id,
                    self.restore_report.get("restored_messages"),
                    self.restore_report.get("restored_event_ids"),
                )

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            runtime, self._runtime = self._runtime, None
            if runtime is not None:
                await runtime.shutdown()

    async def ask(self, *, chat_id: int, prompt: str, tag: str) -> AgentResult:
        return await self._submit(prompt, tag=tag, observation=False)

    async def observe(self, observation: dict[str, Any], *, tag: str,
                      handler: Any | None = None) -> AgentResult:
        """Hand one frozen batch of observations to the agent.

        `handler` is the application's action sink. When present, the agent is
        offered the scoped action tools and its tool calls become durable
        intents instead of plain completion text. Without it, `observe` is just
        a structured prompt and the model cannot act - useful for diagnostics,
        wrong for production.
        """
        return await self._submit(observation, tag=tag, observation=True, handler=handler)

    async def _submit(
        self, payload: Any, *, tag: str, observation: bool, handler: Any | None = None,
    ) -> AgentResult:
        async with self._lock:
            runtime = self._runtime
            if runtime is None:
                raise RuntimeError("agent not started")
            if handler is not None:
                if self._tool_catalog is None:
                    raise RuntimeError("autonomous observation requires a tool catalog")
                self._tool_catalog.bind(handler)
            try:
                delivery = (
                    runtime.submit_observation(payload) if observation
                    else runtime.submit_text(payload)
                )
                if not delivery.accepted:
                    return AgentResult(
                        activation_id="", text=None, status="rejected",
                        detail={"reason": delivery.reason, "tag": tag},
                    )
                input_event_id = delivery.event_id
                activation = await runtime.wait_for_activation(
                    input_event_id=input_event_id, timeout=self._timeout,
                )
                if activation is None:
                    paused = runtime.actor.paused
                    if paused is not None and paused.status == "paused":
                        return AgentResult(
                            activation_id=paused.id, text=None, status="paused",
                            detail={
                                "tag": tag,
                                "input_event_id": input_event_id,
                                "error_kind": paused.error_kind,
                                "error_message": paused.error_message,
                                "pause_hint": runtime.status().get("pause_hint"),
                                "not_this_activation": True,
                            },
                        )
                    return AgentResult(
                        activation_id="", text=None, status="timeout",
                        detail={
                            "tag": tag, "input_event_id": input_event_id,
                            "timeout_seconds": self._timeout,
                        },
                    )

                metrics = activation.context_metrics
                detail = {
                    "tag": tag,
                    "input_event_id": input_event_id,
                    "activation_status": activation.status,
                    "error_kind": activation.error_kind,
                    "usage": metrics.get("provider_usage"),
                    "request_transcript_chars": metrics.get("request_transcript_chars"),
                    "attempts": metrics.get("attempt_count", 0),
                    "attempts_retained": len(metrics.get("attempts") or ()),
                    "attempts_evicted": metrics.get("attempts_evicted", 0),
                }
                if activation.error_kind:
                    detail["error_message"] = activation.error_message
                    detail["pause_hint"] = runtime.status().get("pause_hint")
                    return AgentResult(
                        activation_id=activation.id, text=None, status="error", detail=detail,
                    )

                response = activation.response
                text = response.content if isinstance(response, ModelResponse) else None
                detail["tool_calls"] = [
                    {"id": call.id, "name": call.name}
                    for call in (response.tool_calls if response is not None else ())
                ]
                return AgentResult(
                    activation_id=activation.id, text=text,
                    status=activation.status, detail=detail,
                )
            finally:
                if handler is not None:
                    self._tool_catalog.unbind()

    # -- statistics --------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        runtime = self._runtime
        if runtime is None:
            return {"started": False}
        status = runtime.status()
        return {
            "started": True,
            "session_id": runtime.bus.session_id,
            "activations": runtime.actor.activation_count,
            "actor": status.get("actor"),
            "pause_reason": status.get("pause_reason"),
            "pause_hint": status.get("pause_hint"),
            "transcript_chars": status.get("transcript_chars"),
            "context_limit_chars": status.get("context_limit_chars"),
            "events": runtime.event_count(),
            "python_executions": runtime.event_count("python.requested"),
            "model_tools": [s.get("function", {}).get("name") for s in runtime.actor.tools],
        }


class BackendFailure(Exception):
    """Raised when the provider layer is unusable. Never swallowed silently."""


def build_backend(settings: Settings, *, timeout: float) -> ModelBackend:
    provider = str(settings.provider or "openai")
    if provider in {"openai", "deepseek", "openrouter"} and not settings.api_key:
        raise BackendFailure(
            f"provider {provider!r} requires an API key; set NERVIPULSA_API_KEY"
        )
    return OpenAICompatibleBackend(
        provider=provider,
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
        timeout=timeout,
        adapter=settings.adapter,
    )


__all__ = [
    "AgentAdapter",
    "AgentResult",
    "BackendFailure",
    "EmptyToolCatalog",
    "NervipulsaAgent",
    "ProviderFailure",
    "ScriptedAgent",
    "build_backend",
]
