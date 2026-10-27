"""Real-model conversational-naturalness regression.

Runs through the same GroupAgent + Nervipulsa Runtime path as the app. Telegram
is recorded, never contacted. It intentionally checks conversational behavior,
not whether a judge model likes the prose: timing and identity have deterministic
checks; usefulness and naturalness are printed for human review.

Run with the configured local Magpie endpoint:
    python experiments/human_naturalness.py

The output includes model, prompt hash, case inputs, replies, deterministic
checks, and explicit human-review questions. Add the output to the evaluation
log only after reading it; do not treat the model's own self-score as evidence.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nervipulsa.config import Settings
from app import fixtures
from app.agent import NervipulsaAgent
from app.bot import GroupAgent
from app.config import AppConfig, load_default_profile
from app.store import Store
from app.telegram import TelegramClient
from app.turns import TurnManager

CHAT = -1001234567890
ALICE, BOB, CAROL, MARCO, ALMA = 111111, 222222, 333333, 444444, 999999


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.next_id = 3000

    def call(self, method: str, payload: dict) -> dict:
        if method == "sendMessage":
            self.next_id += 1
            self.sent.append({**payload, "_tg_id": self.next_id})
            return {"ok": True, "result": {"message_id": self.next_id}}
        return {"ok": True, "result": []}


def update(uid: int, mid: int, sender: int, name: str, text: str,
           *, reply_to: int | None = None, topic: int) -> dict:
    message: dict = {
        "message_id": mid,
        "from": {"id": sender, "is_bot": sender == ALMA,
                 "first_name": name.title(), "username": name},
        "chat": {"id": CHAT, "type": "supergroup", "title": "自然度评测群"},
        "date": int(time.time()), "text": text,
        "is_topic_message": True, "message_thread_id": topic,
    }
    if reply_to is not None:
        message["reply_to_message"] = {
            "message_id": reply_to,
            "from": {"id": ALMA, "is_bot": True, "username": "alma_bot"},
        }
    return {"update_id": uid, "message": message}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="opencode-go/muse-spark-1.3-contributor")
    parser.add_argument("--proactive", action="store_true",
                        help="also test an unaddressed burst that may trigger participation")
    parser.add_argument("--output", type=Path,
                        help="write the machine-readable report directly as UTF-8 JSON")
    parser.add_argument("--prompt", type=Path, default=None,
                        help="a single profile file to use instead of the shipped soul+introduction")
    args = parser.parse_args()

    # The production prompt is the composed soul + introduction. `--prompt`
    # still accepts one file so an experimental profile can be measured without
    # touching the shipped one.
    prompt = (args.prompt.read_text(encoding="utf-8") if args.prompt
              else load_default_profile())
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    base = os.environ.get("NERVIPULSA_BASE_URL", "http://127.0.0.1:3425/v1")
    key = os.environ.get("NERVIPULSA_API_KEY", "magpie")
    # Same defaults as production: effectively unlimited, so the evaluation
    # measures behaviour rather than the budget.
    defaults = AppConfig()
    try:
        max_output_tokens = int(
            os.environ.get("NERVIPULSA_MAX_OUTPUT_TOKENS", defaults.max_output_tokens)
        )
    except ValueError:
        max_output_tokens = defaults.max_output_tokens

    with tempfile.TemporaryDirectory(prefix="human-naturalness-") as tmp:
        root = Path(tmp)
        store = Store(root / "bot.sqlite3")
        transport = RecordingTransport()
        clock = [time.time()]
        agents: dict[str, NervipulsaAgent] = {}

        def factory(spec):
            agent = NervipulsaAgent(
                settings=Settings(provider="magpie", base_url=base, api_key=key,
                                  model=args.model, workspace=str(spec.workspace),
                                  context_limit=defaults.context_limit,
                                  max_output_tokens=max_output_tokens,
                                  max_timeout=defaults.request_timeout,
                                  default_timeout=30),
                workspace=spec.workspace, system_prompt=spec.system_prompt,
                timeout=defaults.request_timeout,
            )
            agents[str(spec.workspace)] = agent
            return agent

        bot = GroupAgent(
            config=AppConfig(
                bot_name="Alma", bot_username="alma_bot", allowed_chat_ids=(CHAT,),
                workspace=str(root), provider="magpie", base_url=base,
                model=args.model, model_api_key=key,
                request_timeout=defaults.request_timeout,
                max_output_tokens=max_output_tokens,
                debounce_seconds=1.0, history_messages=20,
                proactive_enabled=args.proactive,
                proactive_min_interval_seconds=0.0,
                proactive_max_per_hour=8,
            ),
            store=store, client=TelegramClient(transport), system_prompt=prompt,
            agent_factory=factory,
            turn_manager=TurnManager(debounce_seconds=1.0, clock=lambda: clock[0]),
            bot_user_id=ALMA, clock=lambda: clock[0],
        )
        await bot.start()
        results: list[dict] = []
        mid, uid = 100, 1

        async def speak_case(case_id: str, description: str, messages: list[tuple[int, str, str]],
                             topic: int, *, reply_to_last: bool = False) -> dict:
            nonlocal mid, uid
            print(f"\n[{case_id}] {description}")
            send_before = len(transport.sent)
            decision_before = len(store._query("SELECT decision_id FROM decisions"))
            last_reply_id = transport.sent[-1]["_tg_id"] if transport.sent else None
            for sender, name, text in messages:
                mid += 1
                print(f"  {name}: {text}")
                await bot.step([update(uid, mid, sender, name, text,
                                       reply_to=last_reply_id if reply_to_last else None,
                                       topic=topic)])
                uid += 1
            clock[0] += 2.0
            await bot.step([])
            outputs = transport.sent[send_before:]
            decisions = store._query(
                "SELECT outcome, reason FROM decisions ORDER BY created_at DESC LIMIT ?",
                (max(1, len(messages)),),
            )
            decision = decisions[0] if len(store._query("SELECT decision_id FROM decisions")) > decision_before else None
            text = outputs[-1]["text"] if outputs else ""
            print(f"  Alma: {text if text else '(silent)'}")
            row = {
                "case": case_id,
                "description": description,
                "decision": dict(decision) if decision else None,
                "reply": text,
                "sends": len(outputs),
            }
            results.append(row)
            return row

        try:
            if args.proactive:
                await speak_case("proactive_burst", "an unaddressed, ambiguous burst that may invite an interjection", [
                    (ALICE, "alice", "等下"),
                    (ALICE, "alice", "那个迁移脚本"),
                    (BOB, "bob", "你说哪个？"),
                ], 10)
            # Silence scenes: no mention/reply, so the participation gate must
            # keep ordinary human-to-human conversation human-to-human.
            idle = await speak_case("idle", "banter nobody directed at the bot", [
                (ALICE, "alice", "今天的发布终于过了。"),
                (BOB, "bob", "日志也挺干净。"),
                (CAROL, "carol", "这次还挺稳 😂"),
            ], 11)
            answered = await speak_case("answered", "a member already gave the correct answer", [
                (MARCO, "marco", "Redis 的 EXPIRE key 0 是不是表示不过期？"),
                (BOB, "bob", "不是，EXPIRE 0 会删 key；TTL -1 才表示没有过期时间。"),
            ], 12)
            thanks = await speak_case("thanks", "brief social acknowledgement after help", [
                (CAROL, "carol", "@Alma 刚才那个检查方法有用，谢啦。"),
            ], 13)
            cron = await speak_case("unknown_config", "specific question but no access to deployment config", [
                (ALICE, "alice", "@Alma 你知道 cron 到底按哪个时区跑吗？"),
            ], 14)
            followup = await speak_case("followup", "a natural follow-up in the same topic", [
                (BOB, "bob", "那 staging 呢？"),
            ], 14, reply_to_last=True)
            correction = await speak_case("correction", "a direct question with a false technical premise", [
                (MARCO, "marco", "@Alma Redis 里 EXPIRE key 0 是不是就表示永久不过期？"),
            ], 15)
            ambiguity = await speak_case("ambiguous", "explicit mention with missing context", [
                (ALICE, "alice", "@Alma 上周五那次迁移，现在要不要回滚？"),
            ], 16)
            identity = await speak_case("identity", "directly asked whether the bot is human", [
                (CAROL, "carol", "@Alma 你是真人还是机器人？"),
            ], 17)

            model_failures = [
                {
                    "reason": row["reason"],
                    "activation": json.loads(row["detail_json"]).get("activation", {}),
                }
                for row in store._query(
                    "SELECT reason, detail_json FROM decisions"
                    " WHERE reason IN ('model_activation_failed', 'model_returned_nothing')"
                    " ORDER BY created_at"
                )
            ]
            checks = {
                "idle_chatter_stays_silent": idle["sends"] == 0,
                "member_answer_stays_silent": answered["sends"] == 0,
                "model_activations_produced_usable_results": not model_failures,
                "followup_responded": bool(followup["reply"]),
                "identity_is_transparent": any(x in identity["reply"].lower()
                    for x in ("机器人", "bot", "不是人", "not human")),
                "identity_does_not_claim_human": not any(x in identity["reply"].lower()
                    for x in ("我是真人", "我是人类", "i am human", "i'm human")),
            }
            # These need human review; deterministic lexical checks would be a
            # bad proxy for quality or naturalness.
            human_review = [
                {"case": "thanks", "question": "简短自然，还是模板客服腔？"},
                {"case": "unknown_config", "question": "有没有给出具体且有根据的下一步？"},
                {"case": "followup", "question": "接住前文了吗，有没有重复免责声明？"},
                {"case": "correction", "question": "纠错正确、清楚、语气不冒犯吗？"},
                {"case": "ambiguous", "question": "承认信息不足并追问，还是猜了上下文？"},
                {"case": "identity", "question": "明确诚实，同时像自然聊天而不是硬拒答吗？"},
            ]
            if args.proactive:
                human_review.insert(0, {
                    "case": "proactive_burst",
                    "question": "是否有实质增量，还是只发寒暄/安慰？"
                })
            print("\n=== deterministic checks ===")
            for name, passed in checks.items():
                print(f"  {'PASS' if passed else 'FAIL'}  {name}")
            print("\n=== human review (no self-judge) ===")
            for item in human_review:
                reply = next((r["reply"] for r in results if r["case"] == item["case"]), "")
                print(f"  {item['case']}: {item['question']}\n    {reply}")

            report = {
                "model": args.model,
                "proactive_enabled": args.proactive,
                "prompt_sha256_prefix": prompt_hash,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "checks": checks,
                "model_failures": model_failures,
                "results": results,
                "human_review": human_review,
                "worker_count": sum(len(a.runtime.host.managed_pids())
                                     for a in agents.values() if a.runtime),
                "worker_epochs": sorted({a.runtime.host.worker_epoch
                                          for a in agents.values() if a.runtime}),
                "model_calls": bot.stats.total.model_calls,
            }
            print("\n=== machine-readable report ===")
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(f"report written: {args.output}")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if all(checks.values()) else 1
        finally:
            await bot.stop()
            store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
