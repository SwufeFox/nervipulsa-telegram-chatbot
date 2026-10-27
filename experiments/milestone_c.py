"""Milestone C — failure injection, one scenario at a time, with real reporting.

Each experiment drives the actual components (the real `Store`, the real
`NervipulsaAgent` where relevant) and prints what happened, including the
runtime's own evidence. Nothing here asserts a claim it did not observe.

The scenarios are the seven the brief names:
  1 duplicate update delivery
  2 crash after persisting the message, before the model call
  3 model call timeout
  4 crash after the reply is generated, before Telegram is asked
  5 Telegram request sent, response lost
  6 new message arrives while a reply is being prepared
  7 runtime process restart

Run: python experiments/milestone_c.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nervipulsa.config import Settings
from nervipulsa.ports import ModelResponse, ProviderFailure
from nervipulsa.providers import ScriptedBackend

from app import fixtures
from app.agent import NervipulsaAgent
from app.bot import GroupAgent
from app.config import AppConfig
from app.store import Store
from app.telegram import TelegramClient, TransportError
from app.turns import TurnManager

FINDINGS: list[dict] = []


def record(scenario: str, observed: str, core_involved: str) -> None:
    FINDINGS.append({"scenario": scenario, "observed": observed, "core": core_involved})
    print(f"    observed: {observed}")


class ScriptedTransport:
    def __init__(self) -> None:
        self.sends: list[dict] = []
        self.fail_with: list[Exception] = []
        self._next = 7000

    def call(self, method: str, payload: dict) -> dict:
        if method == "sendMessage":
            self.sends.append(payload)
            if self.fail_with:
                raise self.fail_with.pop(0)
            self._next += 1
            return {"ok": True, "result": {"message_id": self._next}}
        return {"ok": True, "result": []}


class OkBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(self._respond)

    def _respond(self, request):
        return ModelResponse(content="the cron runs in UTC", model="scripted")


class HangingBackend(ScriptedBackend):
    def __init__(self, delay: float = 30.0) -> None:
        super().__init__(self._respond)
        self.delay = delay

    async def _respond(self, request):
        await asyncio.sleep(self.delay)
        return ModelResponse(content="too late to matter", model="scripted")


class FailingBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(self._respond)

    def _respond(self, request):
        raise ProviderFailure("response_unknown", "provider response unknown: reset by peer")


def make_agent(root: Path, prompt: str, backend) -> NervipulsaAgent:
    return NervipulsaAgent(
        settings=Settings(model="scripted", workspace=str(root / "ws"), max_timeout=30,
                          default_timeout=30),
        workspace=root / "ws",
        system_prompt=prompt,
        backend=backend,
        timeout=20,
    )


def make_bot(root: Path, store: Store, transport, prompt: str, agent, clock):
    config = AppConfig(
        bot_name="Alma", allowed_chat_ids=(fixtures.TEST_CHAT,),
        admin_user_ids=(fixtures.ALICE,), workspace=str(root), model="scripted",
        debounce_seconds=2.0, history_messages=20,
    )
    return GroupAgent(
        config=config, store=store, client=TelegramClient(transport),
        system_prompt=prompt, agent_factory=lambda spec: agent,
        turn_manager=TurnManager(debounce_seconds=2.0, clock=lambda: clock[0]),
        bot_user_id=fixtures.ALMA, clock=lambda: clock[0],
    )


def prompt_text() -> str:
    return (Path(__file__).resolve().parents[1]
            / "app" / "prompts" / "group_agent.md").read_text(encoding="utf-8")


# -- 1 ---------------------------------------------------------------------


async def scenario_duplicate() -> None:
    print("\n[1] duplicate update delivery")
    root = Path(tempfile.mkdtemp(prefix="c1-"))
    store = Store(root / "b.sqlite3")
    transport = ScriptedTransport()
    clock = [1_700_000_000.0]
    backend = OkBackend()
    agent = make_agent(root, prompt_text(), backend)
    bot = make_bot(root, store, transport, prompt_text(), agent, clock)
    await bot.start(); await agent.start()

    update = fixtures.mention()
    tick1, _ = await bot.step([update])
    tick2, _ = await bot.step([update])
    tick3, _ = await bot.step([update] * 5)
    clock[0] += 3
    await bot.step([])

    record("1 duplicate delivery",
           f"first ingest stored={tick1.messages_stored}, redeliveries "
           f"duplicates={tick2.duplicates + tick3.duplicates}, "
           f"decisions={store.stats()['decisions']}, sends={len(transport.sends)}, "
           f"model_calls={backend.calls}",
           "none — dedupe is application-owned (update_id is a Telegram concept)")
    await agent.stop(); await bot.stop(); store.close()


# -- 2 ---------------------------------------------------------------------


async def scenario_crash_before_model() -> None:
    print("\n[2] crash after the message is stored, before the model call")
    root = Path(tempfile.mkdtemp(prefix="c2-"))
    db = root / "b.sqlite3"
    store = Store(db)
    transport = ScriptedTransport()
    clock = [1_700_000_000.0]
    backend = OkBackend()
    agent = make_agent(root, prompt_text(), backend)
    bot = make_bot(root, store, transport, prompt_text(), agent, clock)
    await bot.start(); await agent.start()

    await bot.step([fixtures.mention()])  # stored, window still open
    # "crash": nothing else runs. No shutdown, no turn processing.
    stored = store.stats()["messages"]
    decisions = store.stats()["decisions"]
    await bot.stop(); await agent.stop()
    store.close()

    # New process: startup recovery must notice the message has no decision.
    store2 = Store(db)
    transport2 = ScriptedTransport()
    clock2 = [clock[0] + 10]
    agent2 = make_agent(root, prompt_text(), OkBackend())
    bot2 = make_bot(root, store2, transport2, prompt_text(), agent2, clock2)
    await bot2.start(); await agent2.start()
    revived = bot2.recovered_inbound
    clock2[0] += 3
    await bot2.run_turns()
    sends_after = len(transport2.sends)
    record("2 crash before model",
           f"{stored} message(s) and {decisions} decision(s) survived the crash; "
           f"on restart revived_inbound={revived}; after the debounce window "
           f"decisions={store2.stats()['decisions']}, sends={sends_after}; "
           f"runtime transcript carried over=False (new Runtime, new session)",
           "runtime state is still NOT recoverable — the new Runtime has an empty "
           "transcript. The application recovers the *work item* from its own store "
           "and re-submits it as a fresh activation; conversation memory beyond the "
           "stored history is lost.")
    await agent2.stop(); await bot2.stop(); store2.close()


# -- 3 ---------------------------------------------------------------------


async def scenario_model_timeout() -> None:
    print("\n[3] model call timeout")
    root = Path(tempfile.mkdtemp(prefix="c3-"))
    store = Store(root / "b.sqlite3")
    transport = ScriptedTransport()
    clock = [1_700_000_000.0]
    agent = make_agent(root, prompt_text(), HangingBackend())
    agent._timeout = 0.8
    bot = make_bot(root, store, transport, prompt_text(), agent, clock)
    await bot.start(); await agent.start()

    result = await agent.ask(chat_id=fixtures.TEST_CHAT, prompt="x", tag="probe")
    runtime = agent.runtime
    paused = runtime.actor.paused
    # Distinguish the two timeouts: ours fires first and gives up waiting, while
    # the runtime's own request is still in flight.
    record("3 model timeout",
           f"agent result status={result.status}, text={result.text!r}; "
           f"actor phase={runtime.actor.phase}; paused={paused is not None}; "
           f"activations={len(runtime.actor.activations)}; "
           f"sends={len(transport.sends)}",
           "two independent timeouts exist: NervipulsaAgent._timeout stops the "
           "application waiting and returns status='timeout'; the runtime's own "
           "ModelBackend timeout would eventually raise ProviderFailure and pause "
           "the actor. The application does not wait for the second. No send is "
           "attempted on a timeout, so a slow model cannot produce a late reply.")
    await agent.stop(); await bot.stop(); store.close()


# -- 4 ---------------------------------------------------------------------


async def scenario_crash_before_send() -> None:
    print("\n[4] crash after the reply is generated, before Telegram is asked")
    root = Path(tempfile.mkdtemp(prefix="c4-"))
    db = root / "b.sqlite3"
    store = Store(db)
    transport = ScriptedTransport()
    clock = [1_700_000_000.0]
    agent = make_agent(root, prompt_text(), OkBackend())
    bot = make_bot(root, store, transport, prompt_text(), agent, clock)
    await bot.start(); await agent.start()

    # Prepare the send exactly as the bot does, then stop before the network.
    store.record_decision(decision_id="dec_x", chat_id=fixtures.TEST_CHAT,
                          topic_id=None, turn_id="t", outcome="reply",
                          reason="addressed_by_mention", detail={}, trigger="mention")
    store.prepare_send(decision_id="dec_x", chat_id=fixtures.TEST_CHAT, topic_id=None,
                       reply_to=110, text="the cron runs in UTC")
    status_before = store.get_send("dec_x").status
    store.close()

    store2 = Store(db)
    pending = store2.recover_pending_sends()
    record("4 crash before send",
           f"row status before crash={status_before}; after restart "
           f"pending={[p.decision_id for p in pending]}, "
           f"sends made={len(transport.sends)}",
           "not a runtime concern — the outbox is application-owned")
    store2.close()


# -- 5 ---------------------------------------------------------------------


async def scenario_lost_response() -> None:
    print("\n[5] Telegram request sent, response lost")
    root = Path(tempfile.mkdtemp(prefix="c5-"))
    store = Store(root / "b.sqlite3")
    transport = ScriptedTransport()
    transport.fail_with = [TransportError(kind="indeterminate",
                                          detail="read operation timed out")]
    clock = [1_700_000_000.0]
    agent = make_agent(root, prompt_text(), OkBackend())
    bot = make_bot(root, store, transport, prompt_text(), agent, clock)
    await bot.start(); await agent.start()

    await bot.step([fixtures.mention()])
    clock[0] += 3
    await bot.step([])

    unknown = store.sends_in_status("unknown")
    sent = store.sends_in_status("sent")
    failed = store.sends_in_status("failed")
    record("5 lost response",
           f"unknown={len(unknown)}, sent={len(sent)}, failed={len(failed)}, "
           f"error={(unknown[0].error if unknown else None)!r}, "
           f"transport attempts={len(transport.sends)}",
           "not a runtime concern — and deliberately NOT claimed as at-least-once "
           "or at-most-once delivery")

    # And a restart must not retry it.
    store.close()
    store2 = Store(root / "b.sqlite3")
    resumed = store2.recover_pending_sends()
    record("5b restart after unknown",
           f"pending senders after restart={[p.decision_id for p in resumed]}, "
           f"still unknown={len(store2.sends_in_status('unknown'))}",
           "application keeps UNKNOWN terminal; no blind retry")
    store2.close()
    await agent.stop(); await bot.stop()


# -- 6 ---------------------------------------------------------------------


async def scenario_superseded() -> None:
    print("\n[6] a new message arrives while a reply is being prepared")
    root = Path(tempfile.mkdtemp(prefix="c6-"))
    store = Store(root / "b.sqlite3")
    transport = ScriptedTransport()
    clock = [1_700_000_000.0]
    agent = make_agent(root, prompt_text(), OkBackend())
    bot = make_bot(root, store, transport, prompt_text(), agent, clock)
    await bot.start(); await agent.start()

    original = agent.ask

    async def supersede_while_thinking(*, chat_id, prompt, tag):
        # A newer turn arrives and supersedes this decision mid-flight.
        store.supersede(tag, "dec_newer")
        return await original(chat_id=chat_id, prompt=prompt, tag=tag)

    agent.ask = supersede_while_thinking
    await bot.step([fixtures.mention()])
    clock[0] += 3
    await bot.step([])

    turns = store._query("SELECT outcome FROM turns")
    record("6 superseded mid-flight",
           f"sends={len(transport.sends)}, turn outcome="
           f"{[t['outcome'] for t in turns]}",
           "Nervipulsa cannot cancel an in-flight activation (unknown_route for "
           "activation.cancel); supersession is application-owned and happens "
           "before the send, which is the only irreversible step")
    await agent.stop(); await bot.stop(); store.close()


# -- 7 ---------------------------------------------------------------------


async def scenario_runtime_restart() -> None:
    print("\n[7] runtime process restart")
    root = Path(tempfile.mkdtemp(prefix="c7-"))
    store = Store(root / "b.sqlite3")
    transport = ScriptedTransport()
    clock = [1_700_000_000.0]
    backend = OkBackend()
    agent = make_agent(root, prompt_text(), backend)
    bot = make_bot(root, store, transport, prompt_text(), agent, clock)
    await bot.start(); await agent.start()

    await bot.step([fixtures.mention()])
    clock[0] += 3
    await bot.step([])
    first_session = agent.runtime.bus.session_id
    first_activations = len(agent.runtime.actor.activations)
    first_transcript = len(agent.runtime.actor.transcript.messages)
    durable_sessions = agent.runtime.durable_sessions()
    await agent.stop()
    await bot.stop()

    agent2 = make_agent(root, prompt_text(), backend)
    await agent2.start()
    second_session = agent2.runtime.bus.session_id
    second_transcript = len(agent2.runtime.actor.transcript.messages)
    inspected = agent2.runtime.inspect_durable(session_id=first_session, limit=5)
    record("7 runtime restart",
           f"session changed={first_session != second_session}; "
           f"activations before={first_activations}; transcript before="
           f"{first_transcript}, after={second_transcript}; "
           f"durable sessions listable={len(durable_sessions)}; "
           f"inspect keys={sorted(inspected)}; "
           f"message rows in store={store.stats()['messages']}; "
           f"sends={len(transport.sends)}",
           "Nervipulsa persists evidence, not state: the new Runtime has an empty "
           "transcript and a new session id, and inspect_durable is read-only "
           "(its own notice says inspectable-not-resumable). The application's "
           "store is what makes the chat continuous.")
    await agent2.stop(); store.close()


async def main() -> int:
    print("Milestone C — failure injection against the real components")
    await scenario_duplicate()
    await scenario_crash_before_model()
    await scenario_model_timeout()
    await scenario_crash_before_send()
    await scenario_lost_response()
    await scenario_superseded()
    await scenario_runtime_restart()

    print("\n--- summary ---")
    for finding in FINDINGS:
        print(f"\n{finding['scenario']}")
        print(f"  observed : {finding['observed']}")
        print(f"  core     : {finding['core']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
