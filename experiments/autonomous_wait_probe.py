"""Live probe: does the agent choose to wait, and does the wait wake it?

Scenario: a question is asked but the answer depends on information nobody has
yet. A rule-based bot either answers now (badly) or stays silent forever. The
autonomous agent should be able to register a wait, then act when the missing
information actually arrives.

Telegram is a local recorder only.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import fixtures  # noqa: E402
from app.agent import NervipulsaAgent  # noqa: E402
from app.bot import GroupAgent  # noqa: E402
from app.config import AppConfig, load_config, load_default_profile  # noqa: E402
from app.main import build_settings  # noqa: E402
from app.store import Store  # noqa: E402
from app.telegram import TelegramClient  # noqa: E402
from app.turns import TurnManager  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    def __init__(self) -> None:
        self.sends: list[dict] = []

    async def call(self, method: str, payload: dict) -> dict:
        if method == "sendMessage":
            self.sends.append(payload)
            return {"ok": True, "result": {"message_id": 9000 + len(self.sends)}}
        if method == "getMe":
            return {"ok": True, "result": {"id": fixtures.ALMA, "is_bot": True,
                                           "username": "alma_bot"}}
        return {"ok": True, "result": True}


def msg(uid: int, sender: int, name: str, text: str) -> dict:
    return fixtures.message(update_id=uid, message_id=uid, sender_id=sender,
                            username=name, topic_id=None, text=text)


async def main() -> int:
    base = load_config()
    work = Path(tempfile.mkdtemp(prefix="autonomy-wait-"))
    clock = Clock()
    transport = Recorder()
    store = Store(work / "probe.sqlite3")
    prompt = load_default_profile()
    config = AppConfig(bot_name=base.bot_name, bot_username=base.bot_username or "alma_bot",
                       allowed_chat_ids=(fixtures.TEST_CHAT,), idle_seconds=0.0,
                       workspace=str(work), model=base.model)

    def factory(spec):
        return NervipulsaAgent(
            settings=build_settings(base), workspace=spec.workspace,
            system_prompt=prompt, timeout=base.request_timeout,
            tool_catalog=spec.tool_catalog,
            response_commit_hook=spec.response_commit_hook,
        )

    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=prompt, agent_factory=factory,
                     turn_manager=TurnManager(debounce_seconds=1, clock=clock),
                     bot_user_id=fixtures.ALMA, clock=clock)
    await bot.start()
    try:
        await bot.step([msg(940, fixtures.ALICE, "alice",
                            "线上订单服务从十分钟前开始偶发 502，我还没拿到具体错误码。")])
        clock.advance(5)
        first = await bot.autonomous_step()

        waits = [dict(row) for row in store._query(
            "SELECT wait_id, kind, purpose, event_types_json, status FROM agent_waits")]
        sends_after_first = list(transport.sends)

        await bot.step([msg(941, fixtures.BOB, "bob",
                            "错误码拿到了：上游 Nginx 返回 502，后端连接池被打满。")])
        clock.advance(5)
        second = await bot.autonomous_step()

        waits_after = [dict(row) for row in store._query(
            "SELECT wait_id, status FROM agent_waits")]

        activations = []
        agent = bot._agents.get((fixtures.TEST_CHAT, None))
        if agent is not None and agent.runtime is not None:
            for activation in agent.runtime.actor.activations:
                activations.append({
                    "status": activation.status,
                    "tool_calls": [c.name for c in
                                   (getattr(activation.response, "tool_calls", None) or [])]
                    if activation.response else [],
                })

        print(json.dumps({
            "first_workflow": [{"state": r.state, "outcome": r.outcome,
                                "actions": r.actions, "waits": r.waits} for r in first],
            "waits_registered": waits,
            "sends_after_first": [p.get("text") for p in sends_after_first],
            "second_workflow": [{"state": r.state, "outcome": r.outcome,
                                 "actions": r.actions, "waits": r.waits} for r in second],
            "waits_after": waits_after,
            "all_sends": [p.get("text") for p in transport.sends],
            "activations": activations,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        await bot.stop()
        store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
