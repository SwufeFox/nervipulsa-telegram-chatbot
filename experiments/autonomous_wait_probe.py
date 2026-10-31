"""Live probe: does the agent choose to wait, and does the wait wake it?

Scenario: a question is asked but the answer depends on information nobody has
yet. A rule-based bot either answers now (badly) or stays silent forever. The
autonomous agent should be able to register a wait, then act when the missing
information actually arrives.

Telegram is a local recorder only.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import fixtures  # noqa: E402
from app.agent import NervipulsaAgent  # noqa: E402
from app.bot import GroupAgent  # noqa: E402
from app.config import load_config, load_profile  # noqa: E402
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
    def __init__(self, username: str = "alma_bot") -> None:
        self.username = username
        self.sends: list[dict] = []

    async def call(self, method: str, payload: dict) -> dict:
        if method == "sendMessage":
            self.sends.append(payload)
            return {"ok": True, "result": {"message_id": 9000 + len(self.sends)}}
        if method == "getMe":
            return {"ok": True, "result": {"id": fixtures.ALMA, "is_bot": True,
                                           "username": self.username}}
        return {"ok": True, "result": True}


def msg(uid: int, sender: int, name: str, text: str) -> dict:
    return fixtures.message(update_id=uid, message_id=uid, sender_id=sender,
                            username=name, topic_id=None, text=text)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config.fox.toml")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    base = load_config(args.config)
    if not base.model:
        raise SystemExit("config has no model")
    work = Path(tempfile.mkdtemp(prefix="autonomy-wait-"))
    clock = Clock()
    transport = Recorder(username=base.bot_username or "alma_bot")
    store = Store(work / "probe.sqlite3", clock=clock)
    prompt = load_profile(base)
    config = replace(
        base,
        allowed_chat_ids=(fixtures.TEST_CHAT,),
        idle_seconds=0.0,
        workspace=str(work),
        db_path=str(work / "probe.sqlite3"),
        min_action_interval_seconds=0.0,
    )

    def factory(spec):
        return NervipulsaAgent(
            settings=build_settings(base), workspace=spec.workspace,
            system_prompt=prompt, timeout=base.request_timeout,
            tool_catalog=spec.tool_catalog,
            response_commit_hook=spec.response_commit_hook,
            session_id=spec.session_id,
            restore=spec.restore,
            vision_resolver=spec.vision_resolver,
            vision_observation_check=spec.vision_observation_check,
        )

    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=prompt, agent_factory=factory,
                     turn_manager=TurnManager(debounce_seconds=1, clock=clock),
                     bot_user_id=fixtures.ALMA, clock=clock)
    captured_payloads: list[dict] = []
    original_render = bot.autonomy._render

    def capture_render(observations, chat_id, topic_id, **kwargs):
        rendered = original_render(observations, chat_id, topic_id, **kwargs)
        captured_payloads.append(rendered)
        return rendered

    bot.autonomy._render = capture_render
    try:
        await bot.start()
        await bot.step([msg(
            940, fixtures.ALICE, "alice",
            f"@{config.bot_username} staging 这个 CronJob 的时区我还没确认。"
            "先等我把 YAML 贴出来，再告诉我 spec.timeZone 的值；现在不用答。",
        )])
        clock.advance(5)
        first = await bot.autonomous_step(force=True)

        intent_row_after_first = store.current_intent(fixtures.TEST_CHAT, None)
        intent_after_first = (
            dict(intent_row_after_first) if intent_row_after_first is not None else None
        )
        waits_after_first = [dict(row) for row in store._query(
            "SELECT wait_id, kind, purpose, event_types_json, status FROM agent_waits")]
        sends_after_first = [p.get("text") for p in transport.sends]

        await bot.step([msg(
            941, fixtures.BOB, "bob",
            "我先贴一份参考：apiVersion: batch/v1; kind: CronJob; "
            "spec.timeZone: UTC。",
        )])
        bob_rows = store._query(
            "SELECT sequence FROM observation_events WHERE update_id = ?", (941,))
        if not bob_rows:
            raise RuntimeError("Bob's observation was not recorded")
        bob_sequence = int(bob_rows[0]["sequence"])
        clock.advance(5)
        second = await bot.autonomous_step(force=True)

        intent_row_after_second = store.current_intent(fixtures.TEST_CHAT, None)
        intent_after_second = (
            dict(intent_row_after_second) if intent_row_after_second is not None else None
        )
        waits_after_second = [dict(row) for row in store._query(
            "SELECT wait_id, kind, purpose, event_types_json, status FROM agent_waits")]
        sends_after_second = [p.get("text") for p in transport.sends]

        await bot.step([msg(
            942, fixtures.ALICE, "alice",
            "这是 staging 的 YAML：apiVersion: batch/v1; kind: CronJob; "
            "spec.timeZone: Asia/Shanghai。",
        )])
        alice_rows = store._query(
            "SELECT sequence FROM observation_events WHERE update_id = ?", (942,))
        if not alice_rows:
            raise RuntimeError("Alice's observation was not recorded")
        alice_sequence = int(alice_rows[0]["sequence"])
        clock.advance(5)
        third = await bot.autonomous_step(force=True)

        intent_row_after_third = store.current_intent(fixtures.TEST_CHAT, None)
        intent_after_third = (
            dict(intent_row_after_third) if intent_row_after_third is not None else None
        )
        waits_after_third = [dict(row) for row in store._query(
            "SELECT wait_id, kind, purpose, event_types_json, status FROM agent_waits")]
        workflows = [dict(row) for row in store._query(
            "SELECT workflow_id, episode_id, state, outcome FROM workflows ORDER BY rowid")]
        episodes = [dict(row) for row in store._query(
            "SELECT episode_id, state, reason, intent_id FROM workflow_episodes ORDER BY rowid")]

        activations = []
        agent = bot._agents.get((fixtures.TEST_CHAT, None))
        if agent is not None and agent.runtime is not None:
            for activation in agent.runtime.actor.activations:
                activations.append({
                    "status": activation.status,
                    "error_kind": activation.error_kind,
                    "tool_calls": [{
                        "name": c.name,
                        "arguments": getattr(c, "arguments", {}),
                    } for c in (getattr(activation.response, "tool_calls", None) or [])]
                    if activation.response else [],
                })

        rendered_turns = []
        for payload in captured_payloads:
            episode = payload.get("episode")
            rendered_turns.append({
                "open_intent": payload.get("open_intent"),
                "observation_sequences": [
                    observation.get("sequence")
                    for observation in payload.get("observations", [])
                ],
                "active_waits": payload.get("active_waits"),
                "matched_waits": payload.get("matched_waits"),
                "episode_continuation": (
                    episode.get("continuation") if isinstance(episode, dict) else None
                ),
            })

        same_episode = (
            len(workflows) >= 3
            and workflows[0].get("episode_id") is not None
            and all(row.get("episode_id") == workflows[0].get("episode_id")
                    for row in workflows)
        )
        intent_persisted_after_first = (
            intent_after_first is not None and intent_after_first.get("status") == "open"
        )
        active_wait_ids_before_bob = {
            str(row["wait_id"]) for row in waits_after_first
            if row.get("status") == "active"
        }
        active_wait_ids_after_bob = {
            str(row["wait_id"]) for row in waits_after_second
            if row.get("status") == "active"
        }
        new_active_wait_ids_after_bob = (
            active_wait_ids_after_bob - active_wait_ids_before_bob
        )
        alice_turn_matches = (
            rendered_turns[2].get("matched_waits", [])
            if len(rendered_turns) >= 3 else []
        )
        alice_matched_wait_ids = {
            str(match["wait_id"])
            for match in alice_turn_matches
            if match.get("event_sequence") == alice_sequence
        }
        satisfied_wait_ids_after_alice = {
            str(row["wait_id"]) for row in waits_after_third
            if row.get("status") == "satisfied"
        }
        wait_satisfied_after_alice = bool(
            new_active_wait_ids_after_bob
            & alice_matched_wait_ids
            & satisfied_wait_ids_after_alice
        )
        no_premature_send_after_bob = not sends_after_second
        answer_texts = [str(payload.get("text") or "") for payload in transport.sends]
        single_final_send = len(answer_texts) == 1
        answer_is_correct = (
            single_final_send and "Asia/Shanghai" in answer_texts[0]
        )
        open_intent_after_bob = (
            intent_after_second is not None
            and intent_after_second.get("status") == "open"
        )
        alice_turn_sequences = (
            rendered_turns[2].get("observation_sequences", [])
            if len(rendered_turns) >= 3 else []
        )
        alice_event_in_third_turn = alice_sequence in alice_turn_sequences
        delivered_episode_ids = {
            str(row["episode_id"]) for row in episodes
            if row.get("state") == "closed" and row.get("reason") == "delivered"
        }
        intent_closed_after_delivery = (
            intent_persisted_after_first
            and intent_after_third is None
            and any(
                str(row.get("intent_id")) == str(intent_after_first["intent_id"])
                and str(row.get("episode_id")) in delivered_episode_ids
                for row in episodes
            )
        )
        continuation_completed = (
            same_episode
            and len(rendered_turns) >= 3
            and rendered_turns[1].get("episode_continuation") is True
            and rendered_turns[2].get("episode_continuation") is True
            and intent_persisted_after_first
            and open_intent_after_bob
            and alice_event_in_third_turn
            and no_premature_send_after_bob
            and intent_closed_after_delivery
            and answer_is_correct
        )
        report = {
            "model": base.model,
            "prompt_sha256_prefix": hashlib.sha256(
                prompt.encode("utf-8")).hexdigest()[:12],
            "telegram_transport": "recording_only",
            "first_workflow": [{
                "state": r.state, "outcome": r.outcome,
                "actions": r.actions, "waits": r.waits,
            } for r in first],
            "intent_after_first": intent_after_first,
            "waits_after_first": waits_after_first,
            "sends_after_first": sends_after_first,
            "second_workflow": [{
                "state": r.state, "outcome": r.outcome,
                "actions": r.actions, "waits": r.waits,
                "episode_id": r.episode_id,
            } for r in second],
            "intent_after_second": intent_after_second,
            "waits_after_second": waits_after_second,
            "sends_after_second": sends_after_second,
            "third_workflow": [{
                "state": r.state, "outcome": r.outcome,
                "actions": r.actions, "waits": r.waits,
                "episode_id": r.episode_id,
            } for r in third],
            "intent_after_third": intent_after_third,
            "waits_after_third": waits_after_third,
            "workflows": workflows,
            "episodes": episodes,
            "same_episode_continued": same_episode,
            "intent_persisted_after_first": intent_persisted_after_first,
            "open_intent_after_bob": open_intent_after_bob,
            "new_wait_ids_after_bob": sorted(new_active_wait_ids_after_bob),
            "bob_event_sequence": bob_sequence,
            "alice_event_sequence": alice_sequence,
            "alice_matched_wait_ids": sorted(alice_matched_wait_ids),
            "wait_satisfied_after_alice": wait_satisfied_after_alice,
            "alice_event_in_third_turn": alice_event_in_third_turn,
            "intent_closed_after_delivery": intent_closed_after_delivery,
            "no_premature_send_after_bob": no_premature_send_after_bob,
            "single_final_send": single_final_send,
            "answer_is_correct": answer_is_correct,
            "continuation_completed": continuation_completed,
            "rendered_turns": rendered_turns,
            "all_sends": [p.get("text") for p in transport.sends],
            "activations": activations,
            "model_calls": len(activations),
        }
        serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
            print(f"report written: {args.output}")
        print(serialized, end="")
        return 0 if continuation_completed else 1
    finally:
        try:
            await bot.stop()
        finally:
            store.close()
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
