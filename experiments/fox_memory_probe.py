"""Prove memory across a restart with the real model, not a stub.

Run the same scenario twice in two separate processes against one database:
the first process hears the setup, the second is asked a follow-up whose answer
only exists in the first conversation. A bot without restored memory cannot
answer; this one should.
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
        self.sends: list[str] = []

    async def call(self, method: str, payload: dict) -> dict:
        if method == "sendMessage":
            self.sends.append(payload.get("text", ""))
            return {"ok": True, "result": {"message_id": 9000 + len(self.sends)}}
        if method == "getMe":
            return {"ok": True, "result": {"id": fixtures.ALMA, "is_bot": True,
                                           "username": "linGotFoxbot"}}
        return {"ok": True, "result": True}


def msg(uid: int, text: str) -> dict:
    return fixtures.message(update_id=uid, message_id=uid, sender_id=fixtures.ALICE,
                            username="alice", topic_id=None, text=text)


async def one_process(db: Path, text: str, uid: int, base, prompt: str) -> dict:
    clock = Clock()
    transport = Recorder()
    store = Store(db, clock=clock)
    config = AppConfig(
        bot_name=base.bot_name, bot_username="linGotFoxbot",
        allowed_chat_ids=(CHAT,), idle_seconds=0.0, workspace=str(db.parent),
        model=base.model, min_action_interval_seconds=0.0, max_actions_per_hour=99,
    )
    handle = {}

    def factory(spec):
        handle["agent"] = NervipulsaAgent(
            settings=build_settings(base), workspace=spec.workspace,
            system_prompt=prompt, timeout=base.request_timeout,
            tool_catalog=spec.tool_catalog,
            response_commit_hook=spec.response_commit_hook,
            session_id=spec.session_id, restore=spec.restore,
        )
        return handle["agent"]

    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=prompt, agent_factory=factory,
                     turn_manager=TurnManager(debounce_seconds=1, clock=clock),
                     bot_user_id=fixtures.ALMA, clock=clock)
    await bot.start()
    try:
        await bot.step([msg(uid, text)])
        clock.advance(5)
        results = await bot.autonomous_step()
        agent = handle.get("agent")
        restored = getattr(agent, "restore_report", None) if agent else None
        return {
            "outcome": results[0].outcome if results else None,
            "sent": list(transport.sends),
            "restored": restored,
        }
    finally:
        await bot.stop()
        store.close()


async def main() -> int:
    base = load_config(Path("config.fox.toml"))
    prompt = load_profile(base)
    work = Path(tempfile.mkdtemp(prefix="fox-memory-"))
    db = work / "fox.sqlite3"

    first = await one_process(
        db, "记住，我的猫叫年糕，是只橘猫。", 801, base, prompt)
    second = await one_process(
        db, "我的猫叫什么名字？", 802, base, prompt)

    print(json.dumps({"first_process": first, "second_process": second},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
