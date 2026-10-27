"""Does the per-topic runtime still cost a Python worker?

This is the measurement that motivated the Core change `python_host=False`.
It runs the real application path — GroupAgent, one runtime per (chat, topic),
the same NervipulsaAgent a live deployment uses — and counts worker processes
and spawn time.

Before the Core change, ten topics meant ten resident interpreters that no model
could reach. Run this to see what it is now.

Run: python experiments/worker_cost.py [--topics 10]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nervipulsa.config import Settings
from nervipulsa.ports import ModelResponse
from nervipulsa.providers import ScriptedBackend

from app import fixtures
from app.agent import NervipulsaAgent
from app.bot import GroupAgent
from app.config import AppConfig
from app.store import Store
from app.telegram import TelegramClient
from app.turns import TurnManager


class NullTransport:
    def call(self, method, payload):
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 1}}
        return {"ok": True, "result": []}


def update(uid: int, mid: int, topic: int) -> dict:
    return fixtures.message(update_id=uid, message_id=mid, sender_id=fixtures.ALICE,
                            username="alice", text="Alma ping", topic_id=topic)


async def run(topics: int) -> dict:
    root = Path(tempfile.mkdtemp(prefix="worker-cost-"))
    store = Store(root / "bot.sqlite3")
    clock = [1_700_000_000.0]
    agents: dict[str, NervipulsaAgent] = {}
    spawn_ms: list[float] = []

    def factory(spec):
        agent = NervipulsaAgent(
            settings=Settings(model="scripted", workspace=str(spec.workspace),
                              max_timeout=10, default_timeout=10),
            workspace=spec.workspace,
            system_prompt=spec.system_prompt,
            backend=ScriptedBackend(lambda r: ModelResponse(content="ok", model="s")),
            timeout=10,
        )
        agents[str(spec.workspace)] = agent
        return agent

    bot = GroupAgent(
        config=AppConfig(bot_name="Alma", allowed_chat_ids=(fixtures.TEST_CHAT,),
                         workspace=str(root), model="scripted", debounce_seconds=2.0),
        store=store, client=TelegramClient(NullTransport()), system_prompt="bot",
        agent_factory=factory,
        turn_manager=TurnManager(debounce_seconds=2.0, clock=lambda: clock[0]),
        bot_user_id=fixtures.ALMA, clock=lambda: clock[0],
    )

    started = time.perf_counter()
    await bot.start()
    for index in range(topics):
        await bot.step([update(100 + index, 200 + index, 500 + index)])
    clock[0] += 3.0
    await bot.step([])
    elapsed = time.perf_counter() - started

    pids = {
        Path(ws).name: agent.runtime.host.managed_pids()
        for ws, agent in agents.items() if agent.runtime
    }
    live = sum(len(p) for p in pids.values())
    epochs = {
        Path(ws).name: agent.runtime.host.runtime_facts().get("worker_epoch")
        for ws, agent in agents.items() if agent.runtime
    }
    namespaces = {
        Path(ws).name: agent.runtime.host.runtime_facts().get("namespace")
        for ws, agent in agents.items() if agent.runtime
    }
    python_events = sum(
        1 for a in agents.values() if a.runtime
        for e in a.runtime.trace if e.type == "python.requested"
    )
    statuses = {
        Path(ws).name: a.runtime.status().get("python")
        for ws, a in agents.items() if a.runtime
    }

    # Idle for a while: a lazily-spawning host would show up here.
    await asyncio.sleep(2.0)
    live_idle = sum(len(a.runtime.host.managed_pids()) for a in agents.values() if a.runtime)

    result = {
        "topics": topics,
        "runtimes": len(agents),
        "live_workers": live,
        "live_workers_after_2s_idle": live_idle,
        "wall_time_s": round(elapsed, 2),
        "python_requested_events": python_events,
        "worker_epochs": sorted(set(epochs.values())),
        "namespaces": sorted(set(namespaces.values())),
        "status_python_values": sorted(set(statuses.values())),
    }

    await bot.stop()
    after_stop = sum(len(a.runtime.host.managed_pids()) for a in agents.values() if a.runtime)
    result["live_workers_after_stop"] = after_stop
    store.close()
    return result


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topics", type=int, default=10)
    args = parser.parse_args()

    print(f"One runtime per forum topic — {args.topics} topics, real application path\n")
    result = await run(args.topics)
    for key, value in result.items():
        print(f"  {key:32} {value}")

    print()
    if result["live_workers"] == 0:
        print(f"PASS  {result['runtimes']} runtimes, 0 Python workers")
    else:
        print(f"FAIL  {result['live_workers']} worker(s) still resident")
    return 0 if result["live_workers"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
