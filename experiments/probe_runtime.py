"""Read-only probe: what can an external app actually do with Nervipulsa v0.4-dev?

Every answer here comes from running the real Runtime, not from reading source.
Run: python probe_runtime.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import traceback
from pathlib import Path

from nervipulsa.config import Settings
from nervipulsa.events import SYSTEM_PROMPT, Delivery
from nervipulsa.ports import ModelRequest, ModelResponse, ToolCatalog
from nervipulsa.providers import ScriptedBackend
from nervipulsa.runtime import Runtime

RESULTS: list[tuple[str, str, str]] = []


def record(probe: str, verdict: str, detail: str) -> None:
    RESULTS.append((probe, verdict, detail))


class EmptyCatalog:
    """A catalog exposing zero tools. Used to test the security boundary."""

    def schemas(self, *, workspace: str | None = None):
        return []

    def get(self, name: str):
        return None


def responder(request: ModelRequest) -> ModelResponse:
    return ModelResponse(content="ack", model="scripted")


async def probe_tool_absence() -> None:
    """P1: can an app construct a Runtime with no python_exec at all?"""
    workspace = Path(tempfile.mkdtemp(prefix="probe-tools-"))
    settings = Settings(model="scripted", workspace=str(workspace))
    runtime = Runtime(
        settings,
        ScriptedBackend(responder),
        workspace,
        tool_catalog=EmptyCatalog(),
    )
    await runtime.start()
    runtime.submit_text("hello")
    await runtime.wait_until_idle(timeout=5)
    schemas = runtime.actor.tools
    seen_calls = [e for e in runtime.trace if e.type == "python.requested"]
    await runtime.shutdown()
    verdict = "WORKS" if schemas == [] and not seen_calls else "FAILS"
    record("P1 empty ToolCatalog removes python_exec", verdict, f"actor.tools={schemas!r}")

    # and the default catalog still has it, so the contrast is real
    workspace2 = Path(tempfile.mkdtemp(prefix="probe-tools2-"))
    rt2 = Runtime(Settings(model="scripted", workspace=str(workspace2)), ScriptedBackend(responder), workspace2)
    names = [s["function"]["name"] for s in rt2.actor.tools]
    await rt2.shutdown()
    record("P1b default catalog tool names", "INFO", f"{names!r}")


async def probe_system_prompt() -> None:
    """P2: can an app set its own system prompt through a public parameter?"""
    workspace = Path(tempfile.mkdtemp(prefix="probe-prompt-"))
    settings = Settings(model="scripted", workspace=str(workspace))
    rt = Runtime(settings, ScriptedBackend(responder), workspace, tool_catalog=EmptyCatalog())
    await rt.start()
    rt.submit_text("hi")
    await rt.wait_until_idle(timeout=5)
    snapshot = rt.actor.projector.snapshot()
    system_msg = snapshot[0]
    prompt = system_msg["content"]
    # Look for any constructor parameter that would let an app replace it.
    import inspect
    params = list(inspect.signature(Runtime.__init__).parameters)
    actor_params = list(inspect.signature(type(rt.actor).__init__).parameters)
    await rt.shutdown()
    verdict = "WORKS" if prompt != SYSTEM_PROMPT else "FAILS"
    record("P2 custom system prompt via constructor", verdict, f"prompt={prompt[:60]!r}")
    record("P2b Runtime.__init__ params", "INFO", f"{params}")
    record("P2c LLMActor.__init__ params", "INFO", f"{actor_params}")


async def probe_structured_event() -> None:
    """P3: can an app inject a structured (non user.message) event?"""
    workspace = Path(tempfile.mkdtemp(prefix="probe-event-"))
    settings = Settings(model="scripted", workspace=str(workspace))
    rt = Runtime(settings, ScriptedBackend(responder), workspace, tool_catalog=EmptyCatalog())
    await rt.start()
    results = {}
    for etype in ("chat.message", "external.event", "telegram.update", "user.message"):
        payload = {"text": "x"} if etype == "user.message" else {"chat_id": 1, "text": "x"}
        d = rt.bus.call("app", etype, payload)
        results[etype] = (d.accepted, d.reason)
    # also: is user.message allowed to carry extra structured fields?
    d2 = rt.bus.call("app", "user.message", {"text": "x", "chat_id": -100, "message_id": 7, "topic_id": 3})
    results["user.message+extra"] = (d2.accepted, d2.reason, d2.event_id)
    await rt.wait_until_idle(timeout=5)
    # did the extra fields survive into the model view?
    user_msgs = [m for m in rt.actor.transcript.messages if m.get("role") == "user"]
    await rt.shutdown()
    record("P3 structured external event types", "FAILS" if not any(v[0] for k, v in results.items() if k not in ("user.message", "user.message+extra")) else "WORKS", json.dumps({k: v for k, v in results.items() if k != "user.message+extra"}))
    record("P3b user.message accepts extra fields", "INFO", json.dumps(results["user.message+extra"]))
    record("P3c what the model sees for a tagged message", "INFO", json.dumps(user_msgs[:3]))


async def probe_cancellation() -> None:
    """P4: can an app supersede an in-flight model activation?"""
    workspace = Path(tempfile.mkdtemp(prefix="probe-cancel-"))
    gate = asyncio.Event()

    async def slow_responder(request: ModelRequest) -> ModelResponse:
        await gate.wait()
        return ModelResponse(content="late reply", model="scripted")

    settings = Settings(model="scripted", workspace=str(workspace))
    rt = Runtime(settings, ScriptedBackend(slow_responder), workspace, tool_catalog=EmptyCatalog())
    await rt.start()
    rt.submit_text("first")
    await asyncio.sleep(0.2)  # let the actor pick it up
    bus = rt.bus
    accepted = []
    for etype, payload in (
        ("activation.cancel", {"activation_id": "act_x"}),
        ("agent.cancel", {"activation_id": "act_x"}),
        ("event.supersede", {"event_id": "evt_00000001"}),
    ):
        d = bus.call("app", etype, payload)
        accepted.append((etype, d.accepted, d.reason))
    public = [name for name in dir(rt) if not name.startswith("_")]
    actor_public = [name for name in dir(rt.actor) if not name.startswith("_")]
    # try the one candidate: retry
    d = rt.retry()
    gate.set()
    await asyncio.sleep(0.5)
    await rt.shutdown()
    record("P4 cancel/supersede in-flight activation", "FAILS" if not any(a[1] for a in accepted) else "WORKS", json.dumps(accepted))
    record("P4b Runtime public surface", "INFO", json.dumps(public))
    record("P4c LLMActor public surface", "INFO", json.dumps(actor_public))


async def probe_observation() -> None:
    """P5: can an app observe assistant output and correlate it to input?"""
    workspace = Path(tempfile.mkdtemp(prefix="probe-obs-"))
    settings = Settings(model="scripted", workspace=str(workspace))
    rt = Runtime(settings, ScriptedBackend(responder), workspace, tool_catalog=EmptyCatalog())
    await rt.start()
    seen: list[tuple[str, dict]] = []
    rt.add_listener(lambda e: seen.append((e.type, dict(e.payload))))
    d = rt.submit_text("ping")
    await rt.wait_until_idle(timeout=5)
    await asyncio.sleep(0.2)
    await rt.shutdown()
    assistant = [p for t, p in seen if t == "assistant.message"]
    activations = [(a.id, a.input_event_ids, a.status) for a in rt.actor.activations]
    record("P5 assistant output observable via add_listener", "WORKS" if assistant else "FAILS", json.dumps(assistant))
    record("P5b input event id -> activation correlation", "WORKS" if activations else "FAILS", json.dumps({"submitted": d.event_id, "activations": activations}))


async def probe_empty_response() -> None:
    """P6: what happens when the model deliberately says nothing?"""
    workspace = Path(tempfile.mkdtemp(prefix="probe-empty-"))
    settings = Settings(model="scripted", workspace=str(workspace))

    def silent(request: ModelRequest) -> ModelResponse:
        return ModelResponse(content="", model="scripted")

    rt = Runtime(settings, ScriptedBackend(silent), workspace, tool_catalog=EmptyCatalog())
    await rt.start()
    seen: list[tuple[str, dict]] = []
    rt.add_listener(lambda e: seen.append((e.type, dict(e.payload))))
    rt.submit_text("nobody asked you")
    await rt.wait_until_idle(timeout=5)
    await asyncio.sleep(0.2)
    status = rt.status()
    paused = rt.actor.paused
    await rt.shutdown()
    record("P6 empty model response = silence", "INFO", json.dumps({
        "actor": status["actor"],
        "paused_reason": None if paused is None else paused.error_kind,
        "events": [t for t, _ in seen],
    }))


async def probe_restart() -> None:
    """P7: what actually survives a process restart?"""
    workspace = Path(tempfile.mkdtemp(prefix="probe-restart-"))
    settings = Settings(model="scripted", workspace=str(workspace))
    rt = Runtime(settings, ScriptedBackend(responder), workspace, tool_catalog=EmptyCatalog())
    await rt.start()
    rt.submit_text("remember me")
    await rt.wait_until_idle(timeout=5)
    old_session = rt.bus.session_id
    await rt.shutdown()

    rt2 = Runtime(settings, ScriptedBackend(responder), workspace, tool_catalog=EmptyCatalog())
    await rt2.start()
    fresh_transcript = len(rt2.actor.transcript.messages)
    inspect = rt2.inspect_durable(session_id=old_session, limit=10)
    durable_sessions = rt2.durable_sessions(limit=10)
    await rt2.shutdown()
    record("P7 transcript restored on restart", "FAILS" if fresh_transcript == 0 else "WORKS", f"new transcript messages={fresh_transcript}")
    record("P7b durable inspect of old session", "INFO", json.dumps({"keys": sorted(inspect)}, ensure_ascii=False))
    record("P7c durable sessions listed", "INFO", json.dumps(durable_sessions, ensure_ascii=False)[:400])


async def probe_delivery_saturation() -> None:
    """P8: does a full ui mailbox silently swallow assistant output?"""
    workspace = Path(tempfile.mkdtemp(prefix="probe-sat-"))
    settings = Settings(model="scripted", workspace=str(workspace))
    rt = Runtime(settings, ScriptedBackend(responder), workspace, tool_catalog=EmptyCatalog())
    # Do NOT start: ui_box is not drained, so it accumulates.
    drops = []
    rt.bus.add_delivery_sink(lambda e, r, d: drops.append((r, d.reason)) if not d.accepted else None)
    outcome = []
    for _ in range(300):
        d = rt.bus.call("probe", "assistant.message", {"text": "x", "activation_id": "act_probe"})
        outcome.append(d.accepted)
    rejected = [item for item in drops if item[0] == "ui"]
    record("P8 assistant.message when ui mailbox is full",
           "INFO",
           json.dumps({"accepted": sum(outcome), "rejected": len(outcome) - sum(outcome),
                       "first_reject_reason": rejected[0][1] if rejected else None}))


async def probe_no_tools_runtime_gap() -> None:
    """P9: does the runtime still send an empty tools array to the provider?"""
    workspace = Path(tempfile.mkdtemp(prefix="probe-toolarr-"))
    settings = Settings(model="scripted", workspace=str(workspace))
    backend = ScriptedBackend(responder)
    rt = Runtime(settings, backend, workspace, tool_catalog=EmptyCatalog())
    await rt.start()
    rt.submit_text("hi")
    await rt.wait_until_idle(timeout=5)
    await rt.shutdown()
    req = backend.requests[0] if backend.requests else None
    record("P9 ModelRequest.tools with empty catalog", "INFO",
           json.dumps({"tools": None if req is None else req.tools,
                       "messages_count": None if req is None else len(req.messages),
                       "purpose": None if req is None else req.purpose}))


async def main() -> int:
    probes = [
        probe_tool_absence,
        probe_system_prompt,
        probe_structured_event,
        probe_cancellation,
        probe_observation,
        probe_empty_response,
        probe_restart,
        probe_delivery_saturation,
        probe_no_tools_runtime_gap,
    ]
    for probe in probes:
        try:
            await probe()
        except Exception:
            record(probe.__name__, "ERROR", traceback.format_exc(limit=3))

    print(f"{'PROBE':<52} {'VERDICT':<8} DETAIL")
    print("-" * 110)
    for name, verdict, detail in RESULTS:
        print(f"{name:<52} {verdict:<8} {detail[:400]}")
    failures = [r for r in RESULTS if r[1] == "FAILS"]
    print("-" * 110)
    print(f"{len(RESULTS)} observations, {len(failures)} FAILS, {sum(1 for r in RESULTS if r[1] == 'ERROR')} errors")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
