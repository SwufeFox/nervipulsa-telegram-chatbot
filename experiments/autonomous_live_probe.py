"""One controlled live probe of autonomous participation.

Not an acceptance test and not proof of quality: it answers three narrow
questions against the configured model behind the local gateway.

  1. Does an *unmentioned* group observation reach the real Runtime and produce
     either an action tool call or an explicit finish_silently?
  2. Does a resolved conversation produce no message rather than a redundant
     one?
  3. Does an @ that carries no answerable content stay silent when the agent
     decides so?

All Telegram traffic is a local recorder; nothing is sent anywhere. Only the
model endpoint configured in config.toml is contacted.
"""
from __future__ import annotations

import argparse
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
from app.store import Store  # noqa: E402
from app.telegram import TelegramClient  # noqa: E402
from app.turns import TurnManager  # noqa: E402


class Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    """Records what would be sent; never touches the network."""

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


def scenario(kind: str) -> list[dict]:
    if kind == "unaddressed":
        return [
            fixtures.message(update_id=910, message_id=910, sender_id=fixtures.ALICE,
                             username="alice", topic_id=None,
                             text="我们的 staging 部署一直不出结果，日志里只有一条 504。"),
            fixtures.message(update_id=911, message_id=911, sender_id=fixtures.BOB,
                             username="bob", topic_id=None,
                             text="我刚看了一眼，网关超时是 30 秒，构建其实要跑五分钟。"),
        ]
    if kind == "resolved":
        return [
            fixtures.message(update_id=920, message_id=920, sender_id=fixtures.ALICE,
                             username="alice", topic_id=None,
                             text="生产环境内存一直涨，怎么办？"),
            fixtures.message(update_id=921, message_id=921, sender_id=fixtures.BOB,
                             username="bob", topic_id=None,
                             text="查到了，是缓存没设上限，我刚刚改完并重启了，现在已经稳定。"),
        ]
    if kind == "empty_mention":
        return [
            fixtures.message(update_id=930, message_id=930, sender_id=fixtures.ALICE,
                             username="alice", topic_id=None, text="@alma_bot 👍"),
        ]
    raise SystemExit(f"unknown scenario: {kind}")


async def run(kind: str, config_path: Path | None) -> dict:
    base = load_config(config_path)
    if not base.model:
        raise SystemExit("config has no model")
    work = Path(tempfile.mkdtemp(prefix="autonomy-probe-"))
    clock = Clock()
    transport = Recorder()
    store = Store(work / "probe.sqlite3")
    prompt = load_default_profile()

    config = AppConfig(
        bot_name=base.bot_name, bot_username=base.bot_username or "alma_bot",
        allowed_chat_ids=(fixtures.TEST_CHAT,), dry_run=False,
        idle_seconds=0.0, workspace=str(work), model=base.model,
    )

    def factory(spec):
        from app.main import build_settings

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
        await bot.step(scenario(kind))
        clock.advance(5)
        results = await bot.autonomous_step()
        agent = bot._agents.get((fixtures.TEST_CHAT, None))
        activations = []
        if agent is not None and agent.runtime is not None:
            for activation in agent.runtime.actor.activations:
                metrics = activation.context_metrics or {}
                activations.append({
                    "status": activation.status,
                    "error_kind": activation.error_kind,
                    "usage": metrics.get("provider_usage"),
                    "tool_calls": [
                        {"name": call.name} for call in
                        (getattr(activation.response, "tool_calls", None) or [])
                    ] if activation.response else [],
                })
        return {
            "scenario": kind,
            "model": base.model,
            "workflows": [
                {"state": r.state, "outcome": r.outcome, "actions": r.actions,
                 "waits": r.waits}
                for r in results
            ],
            "sends": [payload.get("text") for payload in transport.sends],
            "activations": activations,
        }
    finally:
        await bot.stop()
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--kind", default="unaddressed",
                        choices=["unaddressed", "resolved", "empty_mention"])
    args = parser.parse_args()
    report = asyncio.run(run(args.kind, args.config))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
