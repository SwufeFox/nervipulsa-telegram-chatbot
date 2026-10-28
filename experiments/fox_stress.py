"""Pressure test: I play five group members and watch what 雾栖 does.

Real model, real autonomous loop, fake Telegram. The goal is not to prove she
is pleasant - it is to find where she breaks.

Questions this answers:
  1. How often does she interrupt in 39 messages of ordinary chatter?
  2. Does she catch the moments that deserve a reply, and skip the ones that
     do not?
  3. Does latency or context size grow in a way that would hurt in practice?
  4. Does she stay in the fox persona under noise, or drift to assistant voice?
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent import NervipulsaAgent  # noqa: E402
from app.bot import GroupAgent  # noqa: E402
from app.config import AppConfig, load_config, load_profile  # noqa: E402
from app.main import build_settings  # noqa: E402
from app.store import Store  # noqa: E402
from app.telegram import TelegramClient  # noqa: E402
from app.turns import TurnManager  # noqa: E402

CHAT = -1007777777777
FOX_ID = 99887766
MEMBERS = {
    "lin": (9001, "林"),
    "kai": (9002, "凯"),
    "momo": (9003, "默默"),
    "zhou": (9004, "周"),
}

# Deliberately mixed: a real problem, small talk, a wrong claim, someone
# tired, a joke, and a clean resolution. Most of it does not need the fox.
SCRIPT: list[tuple[str, str]] = [
    ("lin", "有人用过 ClickHouse 吗，建表一直报 Code 62"),
    ("kai", "Syntax error 吧，看看是不是 ENGINE 写错了"),
    ("lin", "ENGINE = MergeTree() ORDER BY id，我觉得没问题"),
    ("momo", "今天食堂那个红烧肉是真难吃"),
    ("zhou", "哈哈哈 你还没放弃食堂"),
    ("kai", "lin 你那个表名是不是带了大写"),
    ("lin", "全小写"),
    ("momo", "说真的，我宁愿吃泡面"),
    ("zhou", "泡面也是食堂水平"),
    ("momo", "你滚"),
    ("lin", "找到了，是 ReplicatedMergeTree 少了 keeper 配置"),
    ("kai", "那不就报错了吗"),
    ("lin", "它就是报 Code 62，不说原因"),
    ("zhou", "ClickHouse 的错误码文档基本靠猜"),
    ("momo", "所以到底修好没有"),
    ("lin", "好了，加上 keeper 路径就成了"),
    ("kai", "Redis 的 EXPIRE key 0 会立刻删除吧"),
    ("zhou", "对，0 秒等于立即过期"),
    ("momo", "那 -1 呢"),
    ("kai", "负数是立刻删，不是永不过期"),
    ("zhou", "你说的对，不过永不过期是不设 TTL"),
    ("momo", "懂了，我上次就踩过这个坑"),
    ("lin", "我室友昨天通宵写代码，今天请假了"),
    ("zhou", "程序员的身体是消耗品"),
    ("momo", "别说了，我昨天到三点"),
    ("kai", "你们这算养生，我以前连续一周"),
    ("lin", "比惨大赛开始了"),
    ("zhou", "我赢了，我没什么好说的"),
    ("momo", "哈哈哈哈"),
    ("lin", "话说回来，有人知道 k8s CronJob 时区怎么设吗"),
    ("kai", "spec.timeZone 字段，1.27 之后才有"),
    ("zhou", "老版本只能靠容器 TZ，那个不准"),
    ("lin", "我们是 1.29，那应该能用"),
    ("momo", "为什么不能默认用本地时区啊，烦死了"),
    ("kai", "因为集群可能跨时区部署"),
    ("zhou", "而且 UTC 是唯一不会出错的选择"),
    ("momo", "道理都懂，还是烦"),
    ("lin", "好了设完了，谢谢"),
    ("zhou", "散会"),
]


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

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
            return {"ok": True, "result": {"message_id": 80000 + len(self.sends)}}
        if method == "getMe":
            return {"ok": True, "result": {"id": FOX_ID, "is_bot": True,
                                           "username": "linGotFoxbot"}}
        return {"ok": True, "result": True}


def make_update(uid: int, speaker: str, text: str) -> dict:
    sender_id, label = MEMBERS[speaker]
    return {
        "update_id": uid,
        "message": {
            "message_id": uid,
            "chat": {"id": CHAT, "type": "supergroup", "title": "测试群"},
            "from": {"id": sender_id, "is_bot": False, "first_name": label},
            "date": int(time.time()),
            "text": text,
        },
    }


async def main() -> int:
    base = load_config(Path("config.fox.toml"))
    prompt = load_profile(base)
    work = Path(tempfile.mkdtemp(prefix="fox-stress-"))
    clock = Clock()
    transport = Recorder()
    store = Store(work / "bot.sqlite3", clock=clock)
    config = AppConfig(
        bot_name="雾栖", bot_username="linGotFoxbot",
        allowed_chat_ids=(CHAT,), idle_seconds=0.0,
        workspace=str(work), model=base.model,
        # Rate limits off: this run measures behaviour under load, and the
        # limiter has its own unit tests.
        min_action_interval_seconds=0.0, max_actions_per_hour=999,
        max_activations_per_hour=999,
    )
    handle: dict = {}

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
                     bot_user_id=FOX_ID, clock=clock)
    await bot.start()

    timeline: list[dict] = []
    uid = 5000
    started = time.monotonic()
    agent = handle.get("agent")

    def last_decision() -> tuple[list[str], str | None]:
        """What the model actually chose, and whatever text it produced."""
        rt = agent.runtime if agent is not None else None
        if rt is None or not rt.actor.activations:
            return [], None
        act = rt.actor.activations[-1]
        if act.response is None:
            return [], None
        return (
            [c.name for c in (act.response.tool_calls or [])],
            (act.response.content or "").strip()[:120] or None,
        )

    try:
        for speaker, text in SCRIPT:
            uid += 1
            await bot.step([make_update(uid, speaker, text)])
            clock.advance(3)

            before = len(transport.sends)
            t0 = time.monotonic()
            results = await bot.autonomous_step()
            elapsed = time.monotonic() - t0

            calls, content = last_decision()
            rt = agent.runtime if agent is not None else None
            timeline.append({
                "i": len(timeline) + 1,
                "speaker": speaker,
                "said": text,
                "ms": int(elapsed * 1000),
                "outcome": results[0].outcome if results else None,
                "tools": calls,
                "content": content,
                "context_chars": rt.actor.projector.view_length() if rt else None,
                "replied": [p.get("text") for p in transport.sends[before:]],
            })
    finally:
        agent = handle.get("agent")
        usage = {"prompt": 0, "completion": 0, "cached": 0, "reasoning": 0}
        activations = 0
        if agent is not None and agent.runtime is not None:
            for act in agent.runtime.actor.activations:
                activations += 1
                u = (act.context_metrics or {}).get("provider_usage") or {}
                usage["prompt"] += u.get("prompt_tokens") or 0
                usage["completion"] += u.get("completion_tokens") or 0
                usage["reasoning"] += (
                    (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
                )
                usage["cached"] += (
                    (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                )
        latencies = sorted(t["ms"] for t in timeline)
        sent = [p.get("text") for p in transport.sends]
        report = {
            "script_messages": len(SCRIPT),
            "activations": activations,
            "fox_spoke": len(sent),
            "speak_rate_pct": round(100 * len(sent) / len(SCRIPT), 1) if SCRIPT else 0,
            "wall_seconds": round(time.monotonic() - started, 1),
            "latency_ms": {
                "min": latencies[0] if latencies else None,
                "p50": latencies[len(latencies) // 2] if latencies else None,
                "max": latencies[-1] if latencies else None,
            },
            "usage": usage,
            "cache_hit_pct": (
                round(100 * usage["cached"] / usage["prompt"], 1)
                if usage["prompt"] else None
            ),
            "said": sent,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("\n--- who spoke when ---")
        for t in timeline:
            mark = "  >>> " + " / ".join(t["replied"]) if t["replied"] else ""
            why = "·" + ",".join(t["tools"]) if t["tools"] else ""
            print(f"{t['i']:2d} {t['speaker']:5s} {t['said'][:30]:32s}"
                  f" {t['ms']:5d}ms {str(t['outcome']):8s}"
                  f" ctx={t['context_chars'] or 0:>7d}{why}{mark}")
        await bot.stop()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
