"""Second probe pass: the two questions the first pass got wrong or didn't ask."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

from nervipulsa.config import Settings
from nervipulsa.ports import ModelRequest, ModelResponse
from nervipulsa.providers import ScriptedBackend
from nervipulsa.runtime import Runtime


class EmptyCatalog:
    def schemas(self, *, workspace: str | None = None):
        return []

    def get(self, name: str):
        return None


def responder(request: ModelRequest) -> ModelResponse:
    return ModelResponse(content="ack", model="scripted")


async def worker_spawn_cost() -> None:
    """Q1: does start() spawn a Python worker even when no tool exists?"""
    workspace = Path(tempfile.mkdtemp(prefix="probe-worker-"))
    settings = Settings(model="scripted", workspace=str(workspace))
    rt = Runtime(settings, ScriptedBackend(responder), workspace, tool_catalog=EmptyCatalog())
    assert rt.actor.tools == [], "precondition: no tools exposed"

    started = time.perf_counter()
    await rt.start()
    ready = time.perf_counter()
    # Give the scheduler a moment to finish spawning the worker.
    for _ in range(100):
        if rt.host.managed_pids():
            break
        await asyncio.sleep(0.02)
    spawned = time.perf_counter()
    pids = rt.host.managed_pids()
    facts = rt.host.runtime_facts()
    await rt.shutdown()
    print("Q1 start() latency              :", json.dumps({
        "start_ms": round((ready - started) * 1000),
        "worker_pid_present": bool(pids),
        "spawn_observed_ms": round((spawned - started) * 1000),
        "worker_epoch": facts.get("worker_epoch"),
        "namespace": facts.get("namespace"),
    }))


async def ui_saturation() -> None:
    """Q2: with the bus RUNNING but the UI loop not draining, is output dropped?"""
    workspace = Path(tempfile.mkdtemp(prefix="probe-ui-"))
    settings = Settings(model="scripted", workspace=str(workspace))
    rt = Runtime(settings, ScriptedBackend(responder), workspace, tool_catalog=EmptyCatalog())
    # Start only the bus: the ui consumer task never runs, so ui_box fills up.
    rt.bus.start()
    rejections: list[str | None] = []
    rt.bus.add_delivery_sink(
        lambda e, r, d: rejections.append(d.reason) if not d.accepted else None
    )
    accepted = sum(
        1
        for _ in range(300)
        if rt.bus.call("probe", "assistant.message", {"text": "x", "activation_id": "a"}).accepted
    )
    print("Q2 assistant.message into full ui:", json.dumps({
        "accepted": accepted,
        "rejected": 300 - accepted,
        "ordinary_limit": rt.ui_box.ordinary_limit,
        "first_reject_reason": rejections[0] if rejections else None,
    }))
    rt.bus.close()


async def model_visible_envelope() -> None:
    """Q3: exactly what does the model see when the app tags a user.message?"""
    workspace = Path(tempfile.mkdtemp(prefix="probe-env-"))
    settings = Settings(model="scripted", workspace=str(workspace))
    backend = ScriptedBackend(responder)
    rt = Runtime(settings, backend, workspace, tool_catalog=EmptyCatalog())
    await rt.start()
    rt.bus.call(
        "telegram",
        "user.message",
        {"text": "hello", "chat_id": -1001, "message_id": 7, "topic_id": 3, "sender": "alice"},
    )
    await rt.wait_until_idle(timeout=5)
    sent = backend.requests[0].messages
    await rt.shutdown()
    print("Q3 what the provider receives  :", json.dumps(sent, ensure_ascii=False)[:600])


async def main() -> None:
    await worker_spawn_cost()
    await ui_saturation()
    await model_visible_envelope()


if __name__ == "__main__":
    asyncio.run(main())
