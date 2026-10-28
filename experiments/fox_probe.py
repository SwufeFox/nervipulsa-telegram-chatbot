"""Probe 雾栖's actual behaviour through the autonomous loop.

Real model, real personas, fake Telegram. Shows what she would send - and what
she chooses not to - for a handful of group-chat moments.
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
from app.config import AppConfig, load_config, load_profile  # noqa: E402
from app.main import build_settings  # noqa: E402
from app.store import Store  # noqa: E402
from app.telegram import TelegramClient  # noqa: E402
from app.turns import TurnManager  # noqa: E402

ALICE = fixtures.ALICE
BOB = fixtures.BOB
CHAT = fixtures.TEST_CHAT


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


class Recorder:
    def __init__(self) -> None:
        self.sends: list[dict] = []

    async def call(self, method: str, payload: dict) -> dict:
        if method == "sendMessage":
            self.sends.append(payload)
            return {"ok": True, "result": {"message_id": 9000 + len(self.sends)}}
        if method == "getMe":
            return {"ok": True, "result": {"id": fixtures.ALMA, "is_bot": True,
                                           "username": "linGotFoxbot"}}
        return {"ok": True, "result": True}


def msg(uid: int, sender: int, name: str, text: str, topic=None) -> dict:
    return fixtures.message(update_id=uid, message_id=uid, sender_id=sender,
                            username=name, topic_id=topic, text=text)


SCENES = {
    # Someone talks to the group, nobody addresses the fox.
    "idle_chatter": [
        msg(101, ALICE, "alice", "今天好累啊，加到十点。"),
    ],
    # A casual question to the group - is it worth answering?
    "group_question": [
        msg(110, BOB, "bob", "有没有人知道 Redis 的 EXPIRE key 0 是什么意思？"),
        msg(111, ALICE, "alice", "不知道，我只用过 setex。"),
    ],
    # Someone addresses the fox directly.
    "called": [
        msg(120, ALICE, "alice", "狐狸呢？"),
    ],
    # Someone wants to touch the tail - the persona's example case.
    "tail": [
        msg(130, BOB, "bob", "摸摸尾巴"),
    ],
    # A wrong claim the fox could correct.
    "wrong_claim": [
        msg(140, BOB, "bob", "Kubernetes 的 CronJob 默认用 UTC，肯定没问题。"),
    ],
    # Someone is genuinely upset.
    "sad": [
        msg(150, ALICE, "alice", "我妈住院了，这几天可能不太在。"),
    ],
}


async def run_scene(name: str, updates: list[dict], base, prompt: str) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"fox-{name}-"))
    clock = Clock()
    transport = Recorder()
    store = Store(work / "p.sqlite3", clock=clock)
    config = AppConfig(
        bot_name=base.bot_name, bot_username="linGotFoxbot",
        allowed_chat_ids=(CHAT,), idle_seconds=0.0, workspace=str(work),
        model=base.model, min_action_interval_seconds=0.0,
        max_actions_per_hour=100,
    )

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
        await bot.step(updates)
        clock.advance(5)
        results = await bot.autonomous_step()
        agent = bot._agents.get((CHAT, None))
        usage = None
        if agent is not None and agent.runtime is not None:
            for act in agent.runtime.actor.activations:
                u = (act.context_metrics or {}).get("provider_usage")
                if u:
                    usage = u
        return {
            "scene": name,
            "outcome": results[0].outcome if results else None,
            "state": results[0].state if results else None,
            "sent": [p.get("text") for p in transport.sends],
            "usage": usage,
        }
    finally:
        await bot.stop()
        store.close()


async def main() -> int:
    base = load_config(Path("config.fox.toml"))
    prompt = load_profile(base)
    out = []
    for name, updates in SCENES.items():
        out.append(await run_scene(name, updates, base, prompt))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
