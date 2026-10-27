"""A long group conversation against the real model, with throughput measured.

What this is for
----------------
Two questions this answers that the unit tests cannot:

1. **Does it hold up over a long thread?** The fixed naturalness scenarios are
   one turn each. Here five simulated members keep talking across several
   topics for dozens of turns, so the transcript actually grows.
2. **What does it cost per turn?** Wall time, prompt tokens, completion tokens,
   reasoning tokens and — the interesting one — `cached_tokens`, because prompt
   prefix caching is the difference between a cheap long thread and an
   unaffordable one.

Telegram is a recording stand-in: nothing is sent anywhere. The model is real,
and the group members' lines are scripted so the run is reproducible and the
token counts are comparable between runs.

Run:
    python experiments/long_conversation.py
    python experiments/long_conversation.py --turns 60 --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import fixtures  # noqa: E402
from app.agent import NervipulsaAgent  # noqa: E402
from app.bot import GroupAgent  # noqa: E402
from app.config import AppConfig, load_config, load_profile  # noqa: E402
from app.store import Store  # noqa: E402
from app.telegram import TelegramClient  # noqa: E402
from app.turns import TurnManager  # noqa: E402

MODEL = "opencode-go/muse-spark-1.3-contributor"
BASE_URL = "http://127.0.0.1:3425/v1"
API_KEY = os.environ.get("NERVIPULSA_API_KEY", "magpie")


# -- the group ---------------------------------------------------------------
#
# Five members with distinct jobs. The point of giving them roles is that a
# long thread then contains several simultaneous sub-conversations, which is
# where a group agent usually starts answering the wrong person.

ALICE = fixtures.ALICE
BOB = fixtures.BOB
CAROL = 333333
DAVE = 444444
ERIN = 555555

MEMBERS = {ALICE: "alice", BOB: "bob", CAROL: "carol", DAVE: "dave", ERIN: "erin"}


@dataclass
class Utterance:
    """One line of the scripted conversation."""

    sender: int
    text: str
    # `None` = chatter the bot is not part of. `"alma"` = addressed to the bot.
    addresses_bot: bool = False
    # A follow-up to another member, testing whether the bot tracks who asked.
    reply_to_sender: int | None = None


SCRIPT: list[Utterance] = [
    # -- act 1: a deploy goes wrong; Alice is on call -------------------------
    Utterance(ALICE, "deploy 挂了，rollout 卡在 3/5 ready"),
    Utterance(BOB, "又是 probe 那个？"),
    Utterance(ALICE, "不像，这次 pod 起来就 OOMKilled"),
    Utterance(BOB, "那内存 limit 谁改的"),
    Utterance(ALICE, "上周为了修 GC 停顿调到 512Mi，之前是 256Mi"),
    Utterance(BOB, "先回滚吧，别在周五晚上查"),
    Utterance(ALICE, "回滚要多久，我看现在还留了两个 old replica"),
    Utterance(ALICE, "@Alma rolling back a k8s deployment 的时候 maxSurge/maxUnavailable 会影响回滚速度吗", addresses_bot=True),
    Utterance(BOB, "我猜是 image pull 慢"),
    Utterance(ALICE, "先按 Alma 说的试试"),
    Utterance(BOB, "回滚完了，Ready 了"),

    # -- act 2: billing, a different topic and a different pair --------------
    Utterance(CAROL, "账单那边对不上，上午的报表少了差不多 8%"),
    Utterance(DAVE, "少收还是少记"),
    Utterance(CAROL, "少记。支付回调有，但是落到 ledger 的条目缺"),
    Utterance(DAVE, "那先看消费端 lag 吧"),
    Utterance(CAROL, "@Alma kafka consumer group 的 lag 一直涨但消费速率看着正常，一般是哪几种原因", addresses_bot=True),
    Utterance(DAVE, "是不是有 partition 没有 consumer"),
    Utterance(CAROL, "查了，40 个分区都在 assign"),
    Utterance(DAVE, "那可能是 reprocess 的时候 offset commit 提前了"),
    Utterance(CAROL, "我加个监控看看"),

    # -- act 3: a direct question about an earlier answer (memory probe) -----
    Utterance(ERIN, "刚那个回滚的事最后是什么原因"),
    Utterance(ALICE, "内存 limit 太小"),
    Utterance(ERIN, "@Alma 他说的对吗", addresses_bot=True),

    # -- act 4: small talk the bot should not enter --------------------------
    Utterance(BOB, "话说周末有人去看那个展吗"),
    Utterance(DAVE, "哪个"),
    Utterance(BOB, "就是美术馆那个灯光展"),
    Utterance(ERIN, "我去过了，人多"),
    Utterance(DAVE, "票多少钱"),

    # -- act 5: a wrong premise, to see whether it is corrected --------------
    Utterance(CAROL, "@Alma redis 的 EXPIRE key 0 是永不过期吧", addresses_bot=True),
    Utterance(DAVE, "我记得是删掉"),
    Utterance(CAROL, "哦对，我记混了"),

    # -- act 6: cron, a question with a real answer that has a caveat --------
    Utterance(ERIN, "@Alma 我们的备份 cron 写在 k8s CronJob 里，它按哪个时区", addresses_bot=True),
    Utterance(BOB, "容器 TZ 是 UTC"),
    Utterance(ERIN, "所以按 UTC？"),
    Utterance(ERIN, "@Alma 那如果 spec.timeZone 没写呢", addresses_bot=True),

    # -- act 7: someone asks the bot what it is ------------------------------
    Utterance(DAVE, "@Alma 你是真人还是机器人", addresses_bot=True),
    Utterance(BOB, "哈哈"),
    Utterance(DAVE, "我就问问"),

    # -- act 8: back to billing, after a long gap (context retention) -------
    Utterance(CAROL, "lag 那个查到了，是一个 partition 的 consumer 卡在 GC"),
    Utterance(DAVE, "那 8% 能补回来吗"),
    Utterance(CAROL, "重放就行"),
    Utterance(CAROL, "@Alma 重放 kafka 消息补 ledger，怎么保证不重复记账", addresses_bot=True),
    Utterance(DAVE, "幂等 key"),

    # -- act 9: quiet stretch, nothing for the bot ---------------------------
    Utterance(ERIN, "中午吃什么"),
    Utterance(BOB, "楼下那家"),
    Utterance(ERIN, "又吃"),
    Utterance(BOB, "那你说"),
    Utterance(ERIN, "随便"),

    # -- act 10: a final status check, addressed to the bot ------------------
    Utterance(ALICE, "@Alma 今天这几个事你都跟下来了吗", addresses_bot=True),
]


# -- plumbing ----------------------------------------------------------------


class RecordingTransport:
    """Telegram stand-in. Sends are recorded; every response is synthetic."""

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self.api_calls: list[str] = []
        self._next = 5000

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.api_calls.append(method)
        if method == "sendMessage":
            self.sends.append(payload)
            self._next += 1
            return {"ok": True, "result": {"message_id": self._next}}
        if method == "getMe":
            return {"ok": True, "result": {"id": fixtures.ALMA, "is_bot": True,
                                           "username": "alma_bot"}}
        return {"ok": True, "result": []}


@dataclass
class TurnRecord:
    """One activation, as measured from outside."""

    index: int
    utterance: str
    addressed: bool
    outcome: str
    reason: str
    latency_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    prompt_chars: int = 0
    text: str | None = None
    decision_id: str | None = None
    send_status: str | None = None
    turn_id: str | None = None
    turn_outcome: str | None = None
    sender_id: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


def _usage_from_detail(detail: dict[str, Any]) -> dict[str, int]:
    """Pull the token counters out of the activation's provider usage.

    Shape comes from the OpenAI-compatible response, which this endpoint fills
    in including `prompt_tokens_details.cached_tokens` and
    `completion_tokens_details.reasoning_tokens`.
    """
    activation = detail.get("activation") or {}
    usage = activation.get("usage") or {}
    if not isinstance(usage, dict):
        return {}

    def num(value: Any) -> int:
        return value if isinstance(value, int) else 0

    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": num(usage.get("prompt_tokens")),
        "completion_tokens": num(usage.get("completion_tokens")),
        "reasoning_tokens": num(completion_details.get("reasoning_tokens")),
        "cached_tokens": num(prompt_details.get("cached_tokens")),
        "total_tokens": num(usage.get("total_tokens")),
    }


def _build_config(root: Path, history: int) -> AppConfig:
    # Everything token-related comes from the production defaults, so this
    # experiment measures the same budget the bot actually runs with.
    defaults = AppConfig()
    return AppConfig(
        bot_name="Alma",
        bot_username="alma_bot",
        allowed_chat_ids=(fixtures.TEST_CHAT,),
        admin_user_ids=(ALICE,),
        dry_run=False,
        proactive_enabled=False,     # measured separately; this run is passive
        debounce_seconds=2.0,
        history_messages=history,
        workspace=str(root),
        provider="magpie",
        base_url=BASE_URL,
        model=MODEL,
        model_api_key=API_KEY,
        context_limit=defaults.context_limit,
        max_output_tokens=defaults.max_output_tokens,
        request_timeout=defaults.request_timeout,
        soul_path=str(ROOT / "app" / "prompts" / "soul.md"),
        introduction_path=str(ROOT / "app" / "prompts" / "introduction.md"),
    )


def _update(update_id: int, message_id: int, utterance: Utterance,
            reply_to: int | None) -> dict[str, Any]:
    sender = MEMBERS[utterance.sender]
    return fixtures.message(
        update_id=update_id,
        message_id=message_id,
        sender_id=utterance.sender,
        username=sender,
        text=utterance.text,
        topic_id=None,
        reply_to_message_id=reply_to,
        reply_to_sender_id=fixtures.ALMA if reply_to is not None else None,
    )


async def run(turns: int, *, history: int, verbose: bool) -> dict[str, Any]:
    script = SCRIPT[:turns] if turns < len(SCRIPT) else SCRIPT
    root = Path(tempfile.mkdtemp(prefix="long-conversation-"))
    store = Store(root / "bot.sqlite3")
    transport = RecordingTransport()
    config = _build_config(root, history)
    prompt = load_profile(config)

    clock = [1_700_000_000.0]
    agent = NervipulsaAgent(
        settings=_settings(config),
        workspace=root / "chat",
        system_prompt=prompt,
        timeout=config.request_timeout,
    )

    bot = GroupAgent(
        config=config,
        store=store,
        client=TelegramClient(transport),
        system_prompt=prompt,
        agent_factory=lambda spec: agent,   # one topic: one runtime
        turn_manager=TurnManager(debounce_seconds=config.debounce_seconds,
                                 clock=lambda: clock[0]),
        bot_user_id=fixtures.ALMA,
        clock=lambda: clock[0],
    )

    records: list[TurnRecord] = []
    await agent.start()
    await bot.start()

    message_id = 1000
    last_bot_message = None
    seen_decisions: set[str] = set()
    # Pre-seed so nothing written before the run is attributed to turn 0.
    _new_decisions(store, seen_decisions)
    try:
        for index, utterance in enumerate(script):
            message_id += 1
            reply_to = last_bot_message if utterance.reply_to_sender == fixtures.ALMA else None
            await bot.step([_update(index + 1, message_id, utterance, reply_to)])

            # Debounce window has to close before the turn is decided. Advancing
            # the injected clock keeps the run fast without changing behaviour.
            clock[0] += config.debounce_seconds + 0.1
            started = time.perf_counter()
            decisions = await bot.run_turns()
            elapsed = time.perf_counter() - started

            # The activation's token usage is written to the decisions table by
            # `bot._process_turn`, not returned from `run_turns()`, so the
            # measurement is read back from the store. `decisions` still holds
            # the policy verdicts, which is what says reply vs silence.
            stored_rows = _new_decisions(store, seen_decisions)
            stored_by_reason: dict[str, list[dict[str, Any]]] = {}
            for row in stored_rows:
                stored_by_reason.setdefault(row["reason"], []).append(row)

            for decision in decisions:
                record = TurnRecord(
                    index=index,
                    utterance=utterance.text,
                    addressed=utterance.addresses_bot,
                    outcome=decision.outcome,
                    reason=decision.reason,
                    latency_s=round(elapsed, 3),
                    detail=dict(decision.detail),
                    sender_id=utterance.sender,
                )
                bucket = stored_by_reason.get(decision.reason) or []
                stored_row = bucket.pop(0) if bucket else None
                stored = stored_row["detail"] if stored_row else {}
                record.decision_id = stored_row["decision_id"] if stored_row else None
                record.turn_id = stored_row["turn_id"] if stored_row else None
                record.detail.update(stored)
                for key, value in _usage_from_detail(stored).items():
                    setattr(record, key, value)
                record.prompt_chars = int(stored.get("prompt_chars") or 0)
                record.turn_outcome = _turn_outcome(store, record.turn_id)
                if decision.outcome == "reply":
                    # The reply text comes from the *send row for this decision*,
                    # not from "the last thing in the transport". A turn that
                    # failed before sending must not inherit the previous turn's
                    # text — that made a timeout look like a duplicate answer.
                    record.text = _sent_text(store, record.decision_id)
                    record.send_status = _send_status(store, record.decision_id)
                    if record.text is not None and transport.sends:
                        # Remember the id Telegram would have assigned, so a
                        # later utterance can be a reply to the bot's message.
                        last_bot_message = 5000 + len(transport.sends)
                records.append(record)

                if verbose:
                    mark = "→" if record.text else ("…" if decision.outcome == "reply" else "·")
                    print(f"{mark} [{index:02d}] {record.reason:24} {record.latency_s:5.2f}s "
                          f"tok={record.prompt_tokens:5}/{record.completion_tokens:4} "
                          f"cache={record.cached_tokens:5}"
                          + (f"  {record.text[:60]!r}" if record.text else
                             (f"  [{record.send_status}]" if record.send_status else "")))
    finally:
        await bot.stop()
        await agent.stop()
        store.close()

    return _summarise(records, transport, config, root)


def _sent_text(store: Store, decision_id: str | None) -> str | None:
    """The text actually sent for this decision, or None if nothing was sent."""
    if decision_id is None:
        return None
    record = store.get_send(decision_id)
    return record.text if record is not None else None


def _send_status(store: Store, decision_id: str | None) -> str | None:
    if decision_id is None:
        return None
    record = store.get_send(decision_id)
    return record.status if record is not None else None


def _new_decisions(store: Store, seen: set[str]) -> list[dict[str, Any]]:
    """Decision rows written since the last call, oldest first.

    The orchestrator writes the activation metrics (usage, prompt size) into the
    decision row, not into the `Decision` it returns, so measurements are read
    back from the store. Diffing ids is unambiguous even when one turn produces
    several rows (a reply, then a silent `model_activation_failed`).
    """
    rows = store.recent_decisions(fixtures.TEST_CHAT, limit=500)
    fresh = [row for row in rows if row["decision_id"] not in seen]
    fresh.reverse()          # newest-first → oldest-first
    out: list[dict[str, Any]] = []
    for row in fresh:
        seen.add(row["decision_id"])
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except (TypeError, ValueError):
            detail = {}
        out.append({
            "decision_id": row["decision_id"],
            "turn_id": row["turn_id"],
            "reason": row["reason"],
            "detail": detail if isinstance(detail, dict) else {},
        })
    return out


def _turn_outcome(store: Store, turn_id: str | None) -> str | None:
    """Why the turn closed: sent / duplicate / superseded / empty / error / silent.

    This is the difference between "the model said nothing" and "the model said
    something we chose not to send". Without it an unsent reply looks like a
    failure of the model.
    """
    if turn_id is None:
        return None
    rows = store._query("SELECT outcome FROM turns WHERE turn_id = ?", (turn_id,))
    return rows[0]["outcome"] if rows else None


def _settings(config: AppConfig):
    from nervipulsa.config import Settings

    return Settings(
        provider=config.provider,
        base_url=config.base_url,
        model=config.model,
        api_key=config.model_api_key,
        workspace=config.workspace,
        context_limit=config.context_limit,
        max_output_tokens=config.max_output_tokens,
        max_timeout=config.request_timeout,
        default_timeout=min(30.0, config.request_timeout),
    )


def _summarise(records: list[TurnRecord], transport: RecordingTransport,
               config: AppConfig, root: Path) -> dict[str, Any]:
    # A model call is a turn that went as far as asking the runtime — i.e. a
    # reply decision. It is *not* "a turn that reported token usage": failures
    # and timeouts report no usage, and filtering on usage would silently drop
    # exactly the turns that went wrong and inflate every average.
    attempted = [r for r in records if r.outcome == "reply"]
    billed = [r for r in attempted if r.prompt_tokens or r.completion_tokens]
    delivered = [r for r in records if r.send_status == "sent"]
    failed = [r for r in attempted if r.send_status in {"failed", "unknown"}]
    no_model = [r for r in attempted if not (r.prompt_tokens or r.completion_tokens)]
    # A reply the orchestrator chose not to send, and why. Kept separate from
    # send failures: "we decided not to send" is not "the send broke".
    withheld: dict[str, int] = {}
    for record in attempted:
        if record.send_status is None:
            key = record.turn_outcome or "unknown"
            withheld[key] = withheld.get(key, 0) + 1

    def pct(part: float, whole: float) -> float:
        return round(100.0 * part / whole, 1) if whole else 0.0

    prompt_total = sum(r.prompt_tokens for r in billed)
    cached_total = sum(r.cached_tokens for r in billed)
    completion_total = sum(r.completion_tokens for r in billed)
    reasoning_total = sum(r.reasoning_tokens for r in billed)

    latencies = [r.latency_s for r in attempted]
    per_turn_prompt = [r.prompt_tokens for r in billed]
    per_turn_cached = [r.cached_tokens for r in billed]

    return {
        "config": {
            "model": config.model,
            "history_messages": config.history_messages,
            "max_output_tokens": config.max_output_tokens,
            "debounce_seconds": config.debounce_seconds,
            "workspace": str(root),
        },
        "turns": {
            "total": len(records),
            "reply_decisions": len(attempted),
            "replies_sent": len(delivered),
            "silences": sum(1 for r in records if r.outcome != "reply"),
            "model_calls_billed": len(billed),
            "model_calls_without_usage": len(no_model),
            "send_failures": len(failed),
            "replies_withheld": withheld,
            "empty_model_responses": sum(
                1 for r in records if r.reason == "model_returned_nothing"
            ),
            "invalid_model_responses": sum(
                1 for r in records if r.reason == "model_activation_failed"
                and (r.detail.get("activation") or {}).get("error_kind") == "response_invalid"
            ),
        },
        "tokens": {
            "prompt_total": prompt_total,
            "cached_total": cached_total,
            "cache_hit_rate_pct": pct(cached_total, prompt_total),
            "completion_total": completion_total,
            "reasoning_total": reasoning_total,
            "reasoning_share_pct": pct(reasoning_total, completion_total),
            "per_turn_prompt_mean": round(statistics.fmean(per_turn_prompt), 1)
            if per_turn_prompt else 0.0,
            "per_turn_prompt_max": max(per_turn_prompt) if per_turn_prompt else 0,
            "per_turn_cached_mean": round(statistics.fmean(per_turn_cached), 1)
            if per_turn_cached else 0.0,
        },
        "latency": {
            "mean_s": round(statistics.fmean(latencies), 2) if latencies else 0.0,
            "p50_s": round(statistics.median(latencies), 2) if latencies else 0.0,
            "p90_s": round(_percentile(latencies, 0.9), 2) if latencies else 0.0,
            "max_s": round(max(latencies), 2) if latencies else 0.0,
            "replies_per_minute": round(len(delivered) / (sum(latencies) / 60.0), 2)
            if latencies and sum(latencies) else 0.0,
        },
        "cache_curve": [
            {"turn": r.index, "prompt": r.prompt_tokens, "cached": r.cached_tokens,
             "hit_pct": pct(r.cached_tokens, r.prompt_tokens)}
            for r in billed
        ],
        "decisions": [
            {"index": r.index, "outcome": r.outcome, "reason": r.reason,
             "addressed": r.addressed, "latency_s": r.latency_s,
             "prompt_tokens": r.prompt_tokens, "cached_tokens": r.cached_tokens,
             "completion_tokens": r.completion_tokens,
             "reasoning_tokens": r.reasoning_tokens,
             "send_status": r.send_status, "turn_outcome": r.turn_outcome,
             "text": r.text}
            for r in records
        ],
        # The conversation as a reader would see it: every human line, and what
        # the bot did about it. Kept out of `decisions` because a transcript and
        # a metrics table answer different questions.
        "transcript": [
            {
                "index": r.index,
                "speaker": _speaker_of(r),
                "text": r.utterance,
                "bot": r.text,
                "bot_decision": r.outcome,
                "reason": r.reason,
                "turn_outcome": r.turn_outcome,
                "latency_s": r.latency_s,
                "prompt_tokens": r.prompt_tokens,
                "cached_tokens": r.cached_tokens,
                "completion_tokens": r.completion_tokens,
                "reasoning_tokens": r.reasoning_tokens,
            }
            for r in records
        ],
        "telegram": {"send_calls": len(transport.sends)},
    }


def _speaker_of(record: TurnRecord) -> str:
    """Who said this line, from the script the run was built on."""
    return MEMBERS.get(record.sender_id, str(record.sender_id))


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def render_markdown(result: dict[str, Any]) -> str:
    """The run as a readable chat log, with the numbers that belong to each line.

    The JSON is for machines; this is for a person deciding whether the bot is
    any good. Each human line is followed by what the bot did — replied, or
    stayed quiet — and the cost of that decision.
    """
    tokens = result["tokens"]
    turns = result["turns"]
    latency = result["latency"]
    lines = [
        "# 长对话实测记录",
        "",
        f"模型 `{result['config']['model']}`，"
        f"history {result['config']['history_messages']}，"
        f"max_output_tokens {result['config']['max_output_tokens']:,}",
        "",
        "## 吞吐汇总",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 轮次 | {turns['total']} |",
        f"| 决定回复 | {turns['reply_decisions']} |",
        f"| 实际发出 | {turns['replies_sent']} |",
        f"| 沉默 | {turns['silences']} |",
        f"| 计费模型调用 | {turns['model_calls_billed']} |",
        f"| 发送失败 | {turns['send_failures']} |",
        f"| prompt 总 | {tokens['prompt_total']:,} |",
        f"| 命中缓存 | {tokens['cached_total']:,}（{tokens['cache_hit_rate_pct']}%） |",
        f"| completion 总 | {tokens['completion_total']:,} |",
        f"| 其中推理 | {tokens['reasoning_total']:,}（{tokens['reasoning_share_pct']}%） |",
        f"| 延迟 均值/中位/p90/最大 | {latency['mean_s']} / {latency['p50_s']} / "
        f"{latency['p90_s']} / {latency['max_s']} s |",
        f"| 发出速率 | {latency['replies_per_minute']} 条/分钟 |",
        "",
        "## 对话",
        "",
    ]
    for row in result["transcript"]:
        lines.append(f"**{row['speaker']}**: {row['text']}")
        if row["bot"]:
            lines.append(
                f"> 🤖 {row['bot']}  \n"
                f"> `{row['latency_s']}s` · prompt {row['prompt_tokens']:,} "
                f"(cached {row['cached_tokens']:,}) · out {row['completion_tokens']:,} "
                f"(reasoning {row['reasoning_tokens']:,})"
            )
        elif row["bot_decision"] == "reply":
            lines.append(f"> ⚠️ 决定回复但没有发出（`{row['turn_outcome']}`）")
        else:
            lines.append(f"> · 沉默（`{row['reason']}`）")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=len(SCRIPT))
    parser.add_argument("--history", type=int, default=24)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None,
                        help="also write a readable chat log here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(run(args.turns, history=args.history, verbose=not args.quiet))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.quiet
          else json.dumps({k: result[k] for k in ("turns", "tokens", "latency")},
                          ensure_ascii=False, indent=2))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"\nwrote {args.json}")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(result), encoding="utf-8")
        print(f"wrote {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
