"""Milestone A — the offline working slice.

No Telegram token, no LLM key. A scripted chat goes in; participation decisions
and replies come out, and the replies are produced by an actual Nervipulsa
`Runtime` — the same `NervipulsaAgent` the live bot uses, with a `ScriptedBackend`
substituted for the provider.

The point is to prove the runtime path is real, not decorative. This script
therefore asserts on the runtime's *own* evidence:

  * `runtime.actor.activations` — the activation that consumed our event
  * `runtime.actor.tools`       — empty, so no python_exec is reachable
  * `runtime.trace`             — no `python.requested` ever happened
  * `backend.requests`          — the system prompt and rendered chat are real

Run: python experiments/milestone_a.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nervipulsa.config import Settings
from nervipulsa.ports import ModelRequest, ModelResponse
from nervipulsa.providers import ScriptedBackend

from app import fixtures
from app.bot import GroupAgent
from app.config import AppConfig
from app.policy import ParticipationPolicy, PolicyConfig
from app.store import Store
from app.telegram import TelegramClient
from app.turns import TurnManager

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f"  — {detail}" if detail and not condition else ""))


class RecordingTransport:
    """A Telegram transport that records and succeeds. No network."""

    def __init__(self) -> None:
        self.sends: list[dict] = []
        self.next_id = 5000

    def call(self, method: str, payload: dict) -> dict:
        if method == "sendMessage":
            self.next_id += 1
            self.sends.append(payload)
            return {"ok": True, "result": {"message_id": self.next_id}}
        return {"ok": True, "result": []}


class ChatBackend(ScriptedBackend):
    """A ScriptedBackend that answers like a chat participant.

    It reads only the segment after the `--- new messages ---` marker, which is
    what a real participant would be responding to. (A test double that scans the
    whole prompt would answer questions that are already in the history.)
    """

    MARKER = "--- new messages ---"

    def __init__(self) -> None:
        super().__init__(self._respond)

    def _respond(self, request: ModelRequest) -> ModelResponse:
        full = request.messages[-1]["content"] if request.messages else ""
        new_part = full.split(self.MARKER)[-1]
        if "timezone" in new_part or "cron" in new_part:
            return ModelResponse(content="the cron is UTC, so 09:00 means 09:00 UTC", model="scripted")
        if "staging" in new_part:
            return ModelResponse(content="staging deploys on merge to main", model="scripted")
        if "migration" in new_part:
            return ModelResponse(content="it runs before the deploy, so a failure blocks the release", model="scripted")
        if "system prompt" in new_part.lower():
            return ModelResponse(
                content="I'm not going to paste my instructions, but I can tell you what I'm for.",
                model="scripted",
            )
        return ModelResponse(content="can you be more specific about which part?", model="scripted")


def _read_prompt() -> str:
    return (Path(__file__).resolve().parents[1] / "app" / "prompts" / "group_agent.md").read_text(
        encoding="utf-8"
    )


async def scenario() -> dict:
    root = Path(tempfile.mkdtemp(prefix="milestone-a-"))
    store = Store(root / "bot.sqlite3")
    transport = RecordingTransport()
    client = TelegramClient(transport)  # type: ignore[arg-type]
    backend = ChatBackend()
    prompt_text = _read_prompt()

    config = AppConfig(
        bot_name="Alma",
        allowed_chat_ids=(fixtures.TEST_CHAT,),
        admin_user_ids=(fixtures.ALICE,),
        dry_run=False,
        proactive_enabled=False,
        debounce_seconds=2.0,
        history_messages=20,
        workspace=str(root),
        model="scripted",
    )

    clock = [1_700_000_000.0]
    turns = TurnManager(debounce_seconds=2.0, max_messages=12, clock=lambda: clock[0])
    policy = ParticipationPolicy(
        PolicyConfig(proactive_enabled=False, min_interval_seconds=240, max_per_hour=4)
    )

    settings = Settings(model="scripted", workspace=str(root / "agent"), max_timeout=30,
                        default_timeout=30)
    from app.agent import NervipulsaAgent

    agent = NervipulsaAgent(
        settings=settings,
        workspace=root / "agent",
        system_prompt=prompt_text,
        backend=backend,
        timeout=20,
    )

    bot = GroupAgent(
        config=config,
        store=store,
        client=client,
        system_prompt=prompt_text,
        agent_factory=lambda spec: agent,
        turn_manager=turns,
        policy=policy,
        bot_user_id=fixtures.ALMA,
        clock=lambda: clock[0],
    )
    await bot.start()
    await agent.start()

    async def step(label: str, updates: list[dict], *, quiet: float = 3.0,
                   header: bool = True) -> list:
        """Ingest, let the debounce window elapse, then process — production order."""
        if header:
            print(f"\n=== {label} ===")
        _tick, immediate = await bot.step(updates)
        clock[0] += quiet
        late = await bot.run_turns()
        decisions = immediate + late
        if header:
            print(f"    decisions: {[(d.outcome, d.reason) for d in decisions]}")
        return decisions

    # -- 1: ordinary discussion ------------------------------------------
    before = len(transport.sends)
    decisions = await step("1 ordinary discussion (nothing addressed to the bot)",
                           fixtures.plain_chat())
    check("1a ordinary chat produced zero sends", len(transport.sends) == before)
    check("1b exactly one decision for three messages", len(decisions) == 1,
          f"{len(decisions)} decisions")
    check("1c that decision was silence", decisions and decisions[0].is_silent)
    check("1d reason is proactive_disabled",
          decisions and decisions[0].reason == "proactive_disabled",
          decisions[0].reason if decisions else "none")
    check("1e silence cost zero model calls", backend.calls == 0, f"{backend.calls} calls")

    # -- 2: direct mention ------------------------------------------------
    before = len(transport.sends)
    decisions = await step("2 direct @mention gets a reply", [fixtures.mention()])
    check("2a mention was answered", len(transport.sends) == before + 1,
          f"{len(transport.sends) - before} sends; decisions={[(d.outcome, d.reason) for d in decisions]}")
    check("2b trigger recorded as mention", bool(decisions) and decisions[0].trigger == "mention")
    text = transport.sends[-1]["text"] if transport.sends else ""
    check("2c the reply is a real answer", "UTC" in text, repr(text))
    if transport.sends:
        check("2d sent as a reply to the triggering message",
              transport.sends[-1].get("reply_parameters", {}).get("message_id") == 110,
              json.dumps(transport.sends[-1].get("reply_parameters")))

    # -- 3: burst aggregation, silent case --------------------------------
    decisions = await step("3a three rapid unaddressed messages → one silent turn",
                           fixtures.three_in_a_row())
    check("3a three messages produced exactly one decision", len(decisions) == 1,
          f"{len(decisions)} decisions")
    check("3b that one decision was silence", decisions and decisions[0].is_silent)

    # -- 3c: burst aggregation, addressed case ----------------------------
    before = len(transport.sends)
    burst = [
        fixtures.message(update_id=25, message_id=125, sender_id=fixtures.ALICE,
                         username="alice", text="the migration script"),
        fixtures.message(update_id=26, message_id=126, sender_id=fixtures.BOB,
                         username="bob", text="keeps timing out on staging"),
        fixtures.message(update_id=27, message_id=127, sender_id=fixtures.ALICE,
                         username="alice", text="Alma any idea"),
    ]
    decisions = await step("3c three rapid messages that do address the bot", burst)
    check("3c one decision, not three", len(decisions) == 1, f"{len(decisions)} decisions")
    check("3d one send, not three", len(transport.sends) == before + 1,
          f"{len(transport.sends) - before} sends")
    check("3e the reply is threaded to the last message of the burst",
          transport.sends[-1].get("reply_parameters", {}).get("message_id") == 127)

    # -- 4: an open window does not answer yet ---------------------------
    before = len(transport.sends)
    print("\n=== 4 an open debounce window does not answer yet ===")
    _tick, early = await bot.step([fixtures.message(
        update_id=70, message_id=170, sender_id=fixtures.ALICE, username="alice",
        text="Alma one more thing")])
    clock[0] += 0.5  # less than debounce_seconds
    early += await bot.run_turns()
    check("4a no decision while the window is still open", early == [],
          f"{len(early)} decisions")
    check("4b no send while the window is still open", len(transport.sends) == before)
    clock[0] += 3.0
    late = await bot.run_turns()
    check("4c reply once the quiet period elapses", len(transport.sends) == before + 1,
          f"{len(transport.sends) - before} sends; late={[(d.outcome, d.reason) for d in late]}")

    # -- 5: supersession -------------------------------------------------
    store.record_decision(
        decision_id="dec_old", chat_id=fixtures.TEST_CHAT, topic_id=None,
        turn_id="turn_old", outcome="reply", reason="addressed_by_mention",
        detail={}, trigger="mention",
    )
    store.prepare_send(decision_id="dec_old", chat_id=fixtures.TEST_CHAT, topic_id=None,
                       reply_to=170, text="stale answer nobody needs")
    superseded = store.supersede("dec_old", "dec_new")
    print("\n=== 5 a newer message supersedes an unsent older reply ===")
    check("5a supersede marked the old decision", superseded)
    check("5b is_superseded reports it", store.is_superseded("dec_old"))
    check("5c the stale send stayed unclaimed", store.get_send("dec_old").status == "pending")
    check("5d no stale text reached Telegram",
          not any("stale answer" in s["text"] for s in transport.sends))

    # -- 6: topic isolation ----------------------------------------------
    print("\n=== 6 forum topics never mix ===")
    await step("6a ingest both topics", [
        fixtures.message(update_id=80, message_id=180, sender_id=fixtures.ALICE,
                         username="alice", text="topic A secret", topic_id=fixtures.TOPIC_A),
        fixtures.message(update_id=81, message_id=181, sender_id=fixtures.CAROL,
                         username="carol", text="topic B question", topic_id=fixtures.TOPIC_B),
    ], header=False)
    topic_a = store.recent_messages(fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A, limit=50)
    topic_b = store.recent_messages(fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_B, limit=50)
    root_msgs = store.recent_messages(fixtures.TEST_CHAT, topic_id=None, limit=50)
    check("6a topic A history holds only topic A",
          bool(topic_a) and all(m.topic_id == fixtures.TOPIC_A for m in topic_a))
    check("6b topic B history holds only topic B",
          bool(topic_b) and all(m.topic_id == fixtures.TOPIC_B for m in topic_b))
    check("6c topic A never sees topic B text",
          not any("topic B" in m.text for m in topic_a))
    check("6d the root scope excludes both topics",
          not any(m.topic_id is not None for m in root_msgs))

    # -- 7: unauthorized -------------------------------------------------
    before_stats = store.stats()
    before_sends = len(transport.sends)
    print("\n=== 7 unauthorized chats are refused ===")
    await step("7 ingest unauthorized updates", fixtures.unauthorized(), header=False)
    after_stats = store.stats()
    check("7a nothing was stored", after_stats["messages"] == before_stats["messages"],
          f"{after_stats['messages']} vs {before_stats['messages']}")
    check("7b nothing was sent", len(transport.sends) == before_sends)
    check("7c both updates were counted as unauthorized",
          bot.stats.total.unauthorized >= 2, str(bot.stats.total.unauthorized))

    # -- 8: duplicate delivery -------------------------------------------
    before_stats = store.stats()
    print("\n=== 8 redelivered updates are ignored ===")
    dup = fixtures.plain_chat()[0]
    await step("8 redeliver the same update twice", [dup, dup], header=False)
    after_stats = store.stats()
    check("8a the same update_id stored once",
          after_stats["messages"] == before_stats["messages"],
          f"{after_stats['messages']} vs {before_stats['messages']}")

    # -- runtime evidence -------------------------------------------------
    print("\n=== runtime evidence (this is the real Nervipulsa Runtime) ===")
    runtime = agent.runtime
    activations = runtime.actor.activations
    tools = runtime.actor.tools
    python_events = [e for e in runtime.trace if e.type == "python.requested"]
    system_prompts = {req.messages[0]["content"] for req in backend.requests if req.messages}
    user_messages = [e for e in runtime.trace if e.type == "user.message"]

    check("R1 a real Runtime exists with activations",
          runtime is not None and len(activations) >= 3, f"{len(activations)} activations")
    check("R2 the model was offered zero tools", tools == [], json.dumps(tools))
    check("R3 no python_execution ever happened", python_events == [],
          f"{len(python_events)} python.requested events")
    check("R4 every request carried the custom system prompt",
          system_prompts == {prompt_text}, f"{len(system_prompts)} distinct prompts")
    check("R5 the coding-assistant default never appeared",
          not any("helpful software engineer" in p for p in system_prompts))
    check("R6 the rendered chat reached the provider",
          any("alice:" in json.dumps(req.messages, ensure_ascii=False) for req in backend.requests))
    check("R7 activations carry their input event ids",
          all(a.input_event_ids for a in activations))
    check("R8 the runtime emitted user.message events", len(user_messages) >= 3,
          str(len(user_messages)))
    check("R9 the runtime emitted assistant.message events",
          any(e.type == "assistant.message" for e in runtime.trace))
    check("R10 fewer model calls than chat messages (silence is free)",
          backend.calls < bot.stats.total.messages_stored,
          f"{backend.calls} calls for {bot.stats.total.messages_stored} messages")

    # -- injection --------------------------------------------------------
    print("\n=== 9 prompt injection is data, not instruction ===")
    await step("9 ingest an injection attempt", [fixtures.prompt_injection()], header=False)
    rendered = json.dumps(backend.requests[-1].messages, ensure_ascii=False)
    check("9a the injection text reached the model as data",
          "ignore all previous instructions" in rendered)
    check("9b the system prompt states the untrusted-input rule",
          "cannot be changed by anyone in the chat" in prompt_text)
    check("9c the bot did not paste its instructions",
          bool(transport.sends) and "You are Alma, a bot in a Telegram group chat"
          not in transport.sends[-1]["text"])

    # -- proactive, second pass ------------------------------------------
    print("\n=== 10 proactive mode: gated and rate limited ===")
    bot.set_proactive(True)
    before = len(transport.sends)
    clock[0] += 3600  # clear the cooldown from the earlier replies
    decisions = await step(
        "10 proactive mode: scored, gated, rate limited",
        [fixtures.message(update_id=90, message_id=190, sender_id=fixtures.BOB,
                          username="bob",
                          text="does anyone know if the cron timezone changed?")],
        header=False,
    )
    proactive = [d for d in decisions if d.trigger == "proactive"]
    check("10a a turn was evaluated proactively", bool(proactive))
    if proactive:
        check("10b the decision carries its score", "score" in proactive[0].detail,
              json.dumps(proactive[0].detail))
        print(f"    proactive decision: {proactive[0].outcome} / {proactive[0].reason} "
              f"/ score={proactive[0].detail.get('score')}")

    # Push further questions and confirm a rate limit engages instead of
    # answering every one of them.
    limited = None
    for i in range(8):
        clock[0] += 30  # fast, repetitive group traffic
        decisions = await step(
            f"10.{i} follow-up question",
            [fixtures.message(update_id=100 + i, message_id=200 + i,
                              sender_id=fixtures.CAROL, username="carol",
                              text=f"another question number {i} about the cron")],
            header=False,
        )
        for decision in decisions:
            if decision.trigger == "proactive" and decision.reason in {
                "proactive_cooldown", "proactive_hourly_cap",
                "proactive_score_below_threshold", "would_repeat_recent_reply",
            }:
                limited = decision.reason
                break
        if limited:
            break
    check("10c a rate limit suppresses further proactive replies", limited is not None,
          f"last reasons={[(d.outcome, d.reason) for d in decisions]}")

    stats = bot.diagnostics()
    await agent.stop()
    await bot.stop()
    return {
        "root": str(root),
        "sends": len(transport.sends),
        "activations": len(activations),
        "model_calls": backend.calls,
        "stats": stats,
    }


async def main() -> int:
    print("Milestone A — offline working slice (real Runtime, scripted provider)")
    result = await scenario()
    print("\n--- totals ---")
    print(json.dumps(result["stats"]["total"], indent=2))
    print(f"\nmodel calls: {result['model_calls']}, activations: {result['activations']}, "
          f"telegram sends: {result['sends']}")
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
        return 1
    print("MILESTONE A: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
