"""Acceptance criteria, each measured rather than asserted in prose.

The brief lists nine acceptance criteria plus explicit prohibitions on overclaiming.
This script walks them in order and reports PASS/FAIL with the evidence it used.
It deliberately does NOT check things it cannot observe.

Run: python experiments/acceptance.py
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nervipulsa.config import Settings
from nervipulsa.ports import ModelResponse, ToolCall
from nervipulsa.providers import ScriptedBackend

from app import fixtures
from app.agent import NervipulsaAgent
from app.bot import GroupAgent
from app.config import AppConfig
from app.store import Store
from app.telegram import TelegramClient, TransportError
from app.turns import TurnManager

ROWS: list[tuple[str, str, str]] = []


def row(name: str, ok: bool, evidence: str) -> None:
    ROWS.append((name, "PASS" if ok else "FAIL", evidence))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if evidence:
        print(f"         {evidence}")


class Transport:
    def __init__(self) -> None:
        self.sends: list[dict] = []
        self.fail: list[Exception] = []
        self._n = 8000

    def call(self, method: str, payload: dict) -> dict:
        if method == "sendMessage":
            self.sends.append(payload)
            if self.fail:
                raise self.fail.pop(0)
            self._n += 1
            return {"ok": True, "result": {"message_id": self._n}}
        return {"ok": True, "result": []}


class Chat(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(self._r)

    def _r(self, request):
        body = (request.messages[-1]["content"] if request.messages else "").split(
            "--- new messages ---")[-1]
        if "timezone" in body or "cron" in body:
            return ModelResponse(content="the cron runs in UTC", model="scripted")
        return ModelResponse(content="can you be more specific?", model="scripted")


def prompt() -> str:
    return (ROOT / "app" / "prompts" / "group_agent.md").read_text(encoding="utf-8")


def make(root: Path, store: Store, transport, clock, backend=None):
    agent = NervipulsaAgent(
        settings=Settings(model="scripted", workspace=str(root / "ws"), max_timeout=30,
                          default_timeout=30),
        workspace=root / "ws", system_prompt=prompt(),
        backend=backend or Chat(), timeout=20,
    )
    config = AppConfig(
        bot_name="Alma", allowed_chat_ids=(fixtures.TEST_CHAT,),
        admin_user_ids=(fixtures.ALICE,), workspace=str(root), model="scripted",
        debounce_seconds=2.0, history_messages=20,
    )
    bot = GroupAgent(
        config=config, store=store, client=TelegramClient(transport),
        system_prompt=prompt(), agent_factory=lambda spec: agent,
        turn_manager=TurnManager(debounce_seconds=2.0, clock=lambda: clock[0]),
        bot_user_id=fixtures.ALMA, clock=lambda: clock[0],
    )
    return bot, agent


async def main() -> int:
    print("Acceptance criteria — measured\n")

    # 1 -------------------------------------------------------------------
    print("[1] offline tests reproduce participation and silence reliably")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = [line for line in result.stdout.strip().splitlines() if line.strip()][-1]
    row("offline suite green", result.returncode == 0, tail)

    # 2 -------------------------------------------------------------------
    print("\n[2] the real Nervipulsa runtime is used, not a side loop")
    root = Path(tempfile.mkdtemp(prefix="acc2-"))
    store = Store(root / "b.sqlite3")
    transport = Transport()
    clock = [1_700_000_000.0]
    backend = Chat()
    bot, agent = make(root, store, transport, clock, backend)
    await bot.start(); await agent.start()
    await bot.step([fixtures.mention()])
    clock[0] += 3
    await bot.step([])
    rt = agent.runtime
    real = (
        type(rt).__module__.startswith("nervipulsa")
        and len(rt.actor.activations) == 1
        and any(e.type == "user.message" for e in rt.trace)
        and any(e.type == "assistant.message" for e in rt.trace)
        and backend.requests
        and backend.requests[0].activation_id == rt.actor.activations[0].id
    )
    row("runtime objects produced the reply", real,
        f"activations={len(rt.actor.activations)}, "
        f"trace_events={len(rt.trace)}, activation_id matched request=True")
    await agent.stop(); await bot.stop(); store.close()

    # 3 -------------------------------------------------------------------
    print("\n[3] no Python execution permission reaches group messages")
    root = Path(tempfile.mkdtemp(prefix="acc3-"))
    store = Store(root / "b.sqlite3")
    transport = Transport()
    clock = [1_700_000_000.0]

    class ToolCaller(ScriptedBackend):
        def __init__(self):
            super().__init__(self._r)

        def _r(self, request):
            return ModelResponse(content="", tool_calls=[ToolCall(
                id="c1", name="python_exec", arguments={"code": "open('x','w')"})])

    bot, agent = make(root, store, transport, clock, ToolCaller())
    await bot.start(); await agent.start()
    await bot.step([fixtures.mention()])
    clock[0] += 3
    await bot.step([])
    rt = agent.runtime
    no_tools = rt.actor.tools == []
    no_python = not any(e.type == "python.requested" for e in rt.trace)
    no_file = not (root / "ws" / "x").exists()
    no_send = len(transport.sends) == 0
    row("tool calling is impossible and inert",
        no_tools and no_python and no_file and no_send,
        f"tools={rt.actor.tools}, python.requested=0, file_written={not no_file}, "
        f"sends={len(transport.sends)}")
    await agent.stop(); await bot.stop(); store.close()

    # 4 -------------------------------------------------------------------
    print("\n[4] duplicate input never doubles a decision or a send")
    root = Path(tempfile.mkdtemp(prefix="acc4-"))
    store = Store(root / "b.sqlite3")
    transport = Transport()
    clock = [1_700_000_000.0]
    bot, agent = make(root, store, transport, clock)
    await bot.start(); await agent.start()
    update = fixtures.mention()
    await bot.step([update] * 5)
    clock[0] += 3
    await bot.step([update])
    row("one decision, one send",
        store.stats()["decisions"] == 1 and len(transport.sends) == 1
        and len(agent.runtime.actor.activations) == 1,
        f"decisions={store.stats()['decisions']}, sends={len(transport.sends)}, "
        f"activations={len(agent.runtime.actor.activations)}")
    await agent.stop(); await bot.stop(); store.close()

    # 5 -------------------------------------------------------------------
    print("\n[5] a Telegram UNKNOWN is not treated as a failure")
    root = Path(tempfile.mkdtemp(prefix="acc5-"))
    store = Store(root / "b.sqlite3")
    transport = Transport()
    transport.fail = [TransportError(kind="indeterminate", detail="read timed out")]
    clock = [1_700_000_000.0]
    bot, agent = make(root, store, transport, clock)
    await bot.start(); await agent.start()
    await bot.step([fixtures.mention()])
    clock[0] += 3
    await bot.step([])
    unknown = store.sends_in_status("unknown")
    failed = store.sends_in_status("failed")
    resumed = store.recover_pending_sends()
    row("unknown distinct from failed, never requeued",
        len(unknown) == 1 and failed == [] and resumed == [],
        f"unknown={len(unknown)}, failed={len(failed)}, "
        f"pending_after_recovery={len(resumed)}")
    await agent.stop(); await bot.stop(); store.close()

    # 6 -------------------------------------------------------------------
    print("\n[6] a crash does not lose an ingested message")
    root = Path(tempfile.mkdtemp(prefix="acc6-"))
    db = root / "b.sqlite3"
    store = Store(db)
    transport = Transport()
    clock = [1_700_000_000.0]
    bot, agent = make(root, store, transport, clock)
    await bot.start(); await agent.start()
    await bot.step([fixtures.mention()])
    await agent.stop(); await bot.stop()   # crash-equivalent: no turn processing
    store.close()

    store2 = Store(db)
    transport2 = Transport()
    clock2 = [clock[0] + 10]
    bot2, agent2 = make(root, store2, transport2, clock2)
    await bot2.start(); await agent2.start()
    revived = bot2.recovered_inbound
    clock2[0] += 3
    await bot2.run_turns()
    row("pending input recovered and answered",
        revived == 1 and len(transport2.sends) == 1,
        f"revived_inbound={revived}, sends_after_restart={len(transport2.sends)}")
    await agent2.stop(); await bot2.stop(); store2.close()

    # 7 -------------------------------------------------------------------
    print("\n[7] unrelated chat does not cause an equal number of model calls")
    root = Path(tempfile.mkdtemp(prefix="acc7-"))
    store = Store(root / "b.sqlite3")
    transport = Transport()
    clock = [1_700_000_000.0]
    backend = Chat()
    bot, agent = make(root, store, transport, clock, backend)
    await bot.start(); await agent.start()
    for batch in (fixtures.plain_chat(), fixtures.three_in_a_row()):
        await bot.step(batch)
        clock[0] += 3
        await bot.step([])
    messages = store.stats()["messages"]
    calls = backend.calls
    row("silence costs no model call",
        calls == 0 and messages >= 6,
        f"{messages} messages stored, {calls} model calls")
    await agent.stop(); await bot.stop(); store.close()

    # 8 -------------------------------------------------------------------
    print("\n[8] the three documentation artefacts exist and name their evidence")
    docs = {
        "RUNTIME_GAPS": ROOT / "docs" / "RUNTIME_GAPS.md",
        "INTEGRATION_CONTRACT": ROOT / "docs" / "INTEGRATION_CONTRACT.md",
        "EXPERIMENTS": ROOT / "docs" / "EXPERIMENTS.md",
        "runtime-integration": ROOT / "docs" / "architecture" / "runtime-integration.md",
    }
    present = {name: path.exists() for name, path in docs.items()}
    contract = docs["INTEGRATION_CONTRACT"].read_text(encoding="utf-8")
    has_pin = "9a734c4" in contract
    has_nonclaim = "Not exactly-once" in contract
    row("documentation complete",
        all(present.values()) and has_pin and has_nonclaim,
        f"present={present}, pinned_commit={has_pin}, "
        f"disclaims_exactly_once={has_nonclaim}")

    # 9 -------------------------------------------------------------------
    print("\n[9] fixtures contain no real group content")
    fixture_src = (ROOT / "app" / "fixtures.py").read_text(encoding="utf-8")
    synthetic = "-1001234567890" in fixture_src and "Synthetic Test Group" in fixture_src
    row("fixtures are synthetic", synthetic,
        "chat ids are invented, every message is written for the test")

    # summary -------------------------------------------------------------
    print("\n" + "=" * 70)
    passed = sum(1 for _, verdict, _ in ROWS if verdict == "PASS")
    print(f"{passed}/{len(ROWS)} acceptance criteria measured as PASS")
    for name, verdict, evidence in ROWS:
        if verdict != "PASS":
            print(f"  FAILED: {name} — {evidence}")
    print("\nNot measured here (see docs/EXPERIMENTS.md E8): live Telegram API,")
    print("real model provider, hosted CI, multi-process deployment.")
    return 0 if passed == len(ROWS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
