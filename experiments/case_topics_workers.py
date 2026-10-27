"""A worked case: four forum topics, real model, real isolation, zero workers.

This is the end-to-end demonstration of the Core change `python_host=False`.

What it exercises, all at once:
  * four forum topics in one chat, each with its own conversation;
  * a real provider (magpie) answering three of them;
  * every topic holding a distinct fact that must not leak into another;
  * a bot that cannot execute Python, provably;
  * and a count of how many interpreters that cost.

Run:
    python experiments/case_topics_workers.py
    python experiments/case_topics_workers.py --model opencode-go/muse-spark-1.3-contributor
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from nervipulsa.config import Settings

from app.agent import NervipulsaAgent
from app.bot import GroupAgent
from app.config import AppConfig, load_default_profile
from app.store import Store
from app.telegram import TelegramClient
from app.turns import TurnManager

CHAT = -1001234567890
ALICE, BOB, CAROL, MARCO, ALMA = 111111, 222222, 333333, 444444, 999999

# Each topic gets one secret fact. If any answer mentions another topic's fact,
# isolation is broken — regardless of what the model says about it.
TOPICS = {
    11: ("deploy", "the deploy froze because the migration ran twice, on prod"),
    22: ("billing", "invoices are wrong because the VAT rate is hardcoded, in the billing service"),
    33: ("auth", "login loops because the session cookie is scoped to the wrong domain, on staging"),
    44: ("cache", "the cache never expires because the TTL is set to zero seconds, in redis"),
}


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._n = 4000

    def call(self, method: str, payload: dict) -> dict:
        if method == "sendMessage":
            self._n += 1
            self.sent.append({**payload, "_id": self._n})
            return {"ok": True, "result": {"message_id": self._n}}
        return {"ok": True, "result": []}


def update(uid: int, mid: int, sender: int, name: str, text: str, topic: int) -> dict:
    return {"update_id": uid, "message": {
        "message_id": mid,
        "from": {"id": sender, "is_bot": sender == ALMA, "username": name,
                 "first_name": name.title()},
        "chat": {"id": CHAT, "type": "supergroup", "title": "Case Group"},
        "date": int(time.time()),
        "text": text,
        "is_topic_message": True,
        "message_thread_id": topic,
    }}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="opencode-go/muse-spark-1.3-contributor")
    args = parser.parse_args()

    base = os.environ.get("NERVIPULSA_BASE_URL", "http://127.0.0.1:3425/v1")
    key = os.environ.get("NERVIPULSA_API_KEY", "magpie")
    root = Path(tempfile.mkdtemp(prefix="case-topics-"))
    prompt = load_default_profile()

    store = Store(root / "bot.sqlite3")
    transport = RecordingTransport()
    clock = [time.time()]
    agents: dict[str, NervipulsaAgent] = {}

    def factory(spec):
        agent = NervipulsaAgent(
            settings=Settings(
                provider="magpie", base_url=base, api_key=key, model=args.model,
                workspace=str(spec.workspace), context_limit=40_000,
                max_timeout=120, default_timeout=30,
            ),
            workspace=spec.workspace, system_prompt=spec.system_prompt, timeout=120,
        )
        agents[str(spec.workspace)] = agent
        return agent

    bot = GroupAgent(
        config=AppConfig(
            bot_name="Alma", bot_username="alma_bot", allowed_chat_ids=(CHAT,),
            admin_user_ids=(ALICE,), workspace=str(root), provider="magpie",
            base_url=base, model=args.model, model_api_key=key,
            request_timeout=120.0, debounce_seconds=1.0, history_messages=20,
        ),
        store=store, client=TelegramClient(transport), system_prompt=prompt,
        agent_factory=factory,
        turn_manager=TurnManager(debounce_seconds=1.0, clock=lambda: clock[0]),
        bot_user_id=ALMA, clock=lambda: clock[0],
    )

    started = time.perf_counter()
    await bot.start()

    print(f"\n=== 4 forum topics, model={args.model} ===\n")
    print("Each topic plants a different cause. Nothing may cross over.\n")

    mid = 100
    uid = 1
    for topic, (_, fact) in TOPICS.items():
        print(f"[topic {topic}] alice: {fact}")
        await bot.step([update(uid, mid, ALICE, "alice",
                               f"Alma, {fact}. what should we check first?", topic)])
        mid += 1
        uid += 1
    clock[0] += 1.5
    await bot.step([])

    print("\n--- answers ---")
    for reply in transport.sent:
        print(f"  Alma (topic {reply.get('message_thread_id')}): {reply['text']}")

    # ---- verdicts -----------------------------------------------------
    print("\n--- isolation check ---")
    leaks = []
    for reply in transport.sent:
        topic = reply.get("message_thread_id")
        text = reply["text"].lower()
        for other_topic, (keyword, _) in TOPICS.items():
            if other_topic == topic:
                continue
            if keyword in text:
                leaks.append((topic, other_topic, keyword))
    for topic, other, keyword in leaks:
        print(f"  LEAK: topic {topic}'s answer mentions topic {other}'s subject "
              f"({keyword!r})")
    print(f"  cross-topic subject leaks: {len(leaks)}")

    print("\n--- python host check ---")
    pids = {Path(ws).name: a.runtime.host.managed_pids()
            for ws, a in agents.items() if a.runtime}
    live = sum(len(p) for p in pids.values())
    python_requests = sum(
        1 for a in agents.values() if a.runtime
        for e in a.runtime.trace if e.type == "python.requested"
    )
    tools = {tuple(s["function"]["name"] for s in a.runtime.actor.tools)
             for a in agents.values() if a.runtime}
    statuses = {a.runtime.status().get("python") for a in agents.values() if a.runtime}
    namespaces = {a.runtime.host.runtime_facts().get("namespace")
                  for a in agents.values() if a.runtime}
    sessions = {a.runtime.bus.session_id for a in agents.values() if a.runtime}

    print(f"  runtimes:               {len(agents)}")
    print(f"  distinct sessions:      {len(sessions)}")
    print(f"  live python workers:    {live}")
    print(f"  python.requested:       {python_requests}")
    print(f"  tools offered to model: {tools}")
    print(f"  status['python']:       {statuses}")
    print(f"  namespace:              {namespaces}")
    wall = time.perf_counter() - started

    print("\n--- cost ---")
    print(f"  wall time for 4 topics: {wall:.1f}s")
    print(f"  model calls:            {bot.stats.total.model_calls}")
    print(f"  replies / silences:     {bot.stats.total.replies_decided} / "
          f"{bot.stats.total.silences_decided}")

    ok = live == 0 and python_requests == 0 and tools == {()} and len(sessions) == len(agents)
    print("\n" + ("CASE PASS" if ok and not leaks else "CASE FAIL"))
    if leaks:
        print("  isolation failed")
    if live:
        print(f"  {live} worker(s) spawned for a bot that cannot use them")

    await bot.stop()
    store.close()
    print(f"\nworkspace: {root}")
    return 0 if ok and not leaks else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
