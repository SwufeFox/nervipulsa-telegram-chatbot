"""The only place that talks to Nervipulsa.

One `Runtime` per chat. The runtime provides the event kernel, the activation
ledger, the transcript, and the provider adapter; this module supplies the
identity (system prompt), the input (one rendered prompt string), and the
observation (the assistant message for a given activation).

Why one Runtime per chat rather than one global Runtime:

* `Transcript` is append-only for the life of a Runtime. Two groups sharing one
  Runtime would share a model view, and the runtime offers no way to partition
  it. Separate Runtimes is the strongest isolation available.
* `Runtime.submit_text` carries no chat identity, so a shared Runtime could not
  attribute output to a chat even if the transcript were partitioned.

Cost: one `Runtime.start()` per chat spawns a Python worker each (measured in
docs/architecture/runtime-integration.md §3 G2). For a handful of authorized
chats that is acceptable; `RUNTIME_GAPS.md` records the gap.

Tools: this module constructs an `EmptyCatalog`. The model is never offered
`python_exec` or `python_environment`, and a `python.requested` event can never
be produced by a model response. The security tests assert this.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from nervipulsa.config import Settings
from nervipulsa.ports import ModelBackend, ModelRequest, ModelResponse, ProviderFailure
from nervipulsa.providers import OpenAICompatibleBackend
from nervipulsa.runtime import Runtime

log = logging.getLogger(__name__)


class EmptyToolCatalog:
    """Exposes no tools. This is the security boundary for this application."""

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

    The critical property is that a reply is read by *activation*, not by
    listening to `assistant.message`. That matters because `assistant.message`
    can be dropped when the UI mailbox fills (docs §3 G5). Reading the activation
    ledger for the id we just waited on is unambiguous: it is either there or the
    activation is still running.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        workspace: Path,
        system_prompt: str,
        backend: ModelBackend | None = None,
        timeout: float = 60.0,
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
        self._runtime: Runtime | None = None
        self._lock = asyncio.Lock()

    @property
    def runtime(self) -> Runtime | None:
        return self._runtime

    async def start(self) -> None:
        if self._runtime is not None:
            return
        runtime = Runtime(
            self._settings,
            self._backend,
            self._workspace,
            tool_catalog=EmptyToolCatalog(),
            system_prompt=self._system_prompt,
            # No tool is exposed, so nothing in this application can submit
            # Python. Declining the host means no interpreter is spawned and
            # `python.requested` is not even routable. This matters because the
            # bot keeps one runtime per (chat, topic): with a host per runtime,
            # ten forum topics meant ten resident interpreters.
            python_host=False,
        )
        await runtime.start()
        self._runtime = runtime
        # Fail loudly at startup rather than on the first message: if the tools
        # are not empty, the security boundary is broken.
        exposed = [schema.get("function", {}).get("name") for schema in runtime.actor.tools]
        if exposed:
            await runtime.shutdown()
            self._runtime = None
            raise RuntimeError(f"runtime exposed tools to the model: {exposed}")

    async def stop(self) -> None:
        runtime, self._runtime = self._runtime, None
        if runtime is not None:
            await runtime.shutdown()

    async def ask(self, *, chat_id: int, prompt: str, tag: str) -> AgentResult:
        """Submit one prompt and wait for the activation it produced.

        Serialized per chat: the runtime processes one activation at a time
        anyway, and holding the lock makes "which activation answered me"
        unambiguous.
        """
        if self._runtime is None:
            raise RuntimeError("agent not started")
        runtime = self._runtime

        async with self._lock:
            before = len(runtime.actor.activations)
            paused_before = runtime.actor.paused
            prior_attempts = len((getattr(paused_before, "context_metrics", None) or {}).get("attempts") or [])
            delivery = runtime.submit_text(prompt)
            if not delivery.accepted:
                return AgentResult(
                    activation_id="",
                    text=None,
                    status="rejected",
                    detail={"reason": delivery.reason, "tag": tag},
                )
            input_event_id = delivery.event_id

            activation = await self._await_activation(
                runtime, before, input_event_id, paused_before, prior_attempts
            )
            if activation is None:
                paused = runtime.actor.paused
                if paused is not None:
                    # Distinguishable from a timeout: the actor refused to run,
                    # which is an operator problem, not a slow provider.
                    return AgentResult(
                        activation_id=paused.id,
                        text=None,
                        status="paused",
                        detail={
                            "tag": tag,
                            "input_event_id": input_event_id,
                            "error_kind": paused.error_kind,
                            "error_message": paused.error_message,
                            "not_this_activation": True,
                        },
                    )
                return AgentResult(
                    activation_id="",
                    text=None,
                    status="timeout",
                    detail={"tag": tag, "input_event_id": input_event_id,
                            "timeout_seconds": self._timeout},
                )

            metrics = dict(getattr(activation, "context_metrics", {}) or {})
            detail = {
                "tag": tag,
                "input_event_id": input_event_id,
                "activation_status": activation.status,
                "error_kind": activation.error_kind,
                "usage": metrics.get("provider_usage"),
                "request_transcript_chars": metrics.get("request_transcript_chars"),
                "attempts": len(metrics.get("attempts") or []),
            }

            if activation.error_kind:
                return AgentResult(
                    activation_id=activation.id, text=None,
                    status="error", detail=detail,
                )

            response = getattr(activation, "response", None)
            text = response.content if isinstance(response, ModelResponse) else None
            return AgentResult(
                activation_id=activation.id, text=text,
                status=activation.status, detail=detail,
            )

    async def _await_activation(
        self, runtime: Runtime, before: int, input_event_id: str | None,
        paused_before: Any | None, prior_attempts: int,
    ) -> Any | None:
        """Poll the activation ledger for the activation that consumed our event.

        This is a live read of `actor.activations`, which is the only
        input→output correlation the runtime exposes (docs §2). If the runtime
        ever grows a durable correlation event, replace this loop with it.
        """
        deadline = asyncio.get_running_loop().time() + self._timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
            activations = runtime.actor.activations
            for activation in activations[before:]:
                if input_event_id and input_event_id in activation.input_event_ids:
                    if activation.status in {"running"}:
                        continue
                    return activation
            # A prior pause may still be visible while the actor asynchronously
            # handles this user message. Core only blocks automatic recovery for
            # these two kinds; for other kinds, wait for the correlated input.
            # If retrying the old activation has already failed again, there is
            # no reason to wait for the held input until the full timeout.
            paused = runtime.actor.paused
            if paused is not None:
                if paused is not paused_before or paused.error_kind in {
                    "response_unknown", "commit_interrupted"
                }:
                    return None
                attempts = len((getattr(paused, "context_metrics", None) or {}).get("attempts") or [])
                if paused.status == "paused" and attempts > prior_attempts:
                    return None
        return None

    # -- statistics --------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        runtime = self._runtime
        if runtime is None:
            return {"started": False}
        status = runtime.status()
        return {
            "started": True,
            "session_id": runtime.bus.session_id,
            "activations": len(runtime.actor.activations),
            "actor": status.get("actor"),
            "pause_reason": status.get("pause_reason"),
            "transcript_chars": status.get("transcript_chars"),
            "context_limit_chars": status.get("context_limit_chars"),
            "events": len(runtime.trace),
            "python_executions": rt_count(runtime, "python.requested"),
            "model_tools": [s.get("function", {}).get("name") for s in runtime.actor.tools],
        }


def rt_count(runtime: Runtime, event_type: str) -> int:
    return sum(1 for event in runtime.trace if event.type == event_type)


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
