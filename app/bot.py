"""The orchestrator: durable inbound → turn → decision → runtime → durable outbox.

This is the one module that knows the whole flow. It is deliberately a single
class rather than seven services: the transitions are short and the ordering
between them is the thing being tested, so splitting them would hide the
ordering in wiring.

Sequence for one poll batch:

    for each update:
        authorize chat         (unauthorized → dropped, counted, no row)
        dedupe on update_id    (duplicate → dropped, counted, no row)
        persist update + message                        [durable]
    advance poll offset                                 [durable, after input]

    ready turns = turn_manager.ready()                  [debounce]
    for each turn:
        policy.decide()                                 [deterministic first]
        record decision (reply OR silent)                [durable]
        if reply:
            prepare send row (status=pending)            [durable]
            claim → status=sending                       [durable, before network]
            await agent.ask(...)                         [runtime]
            check superseded / duplicate
            send via Telegram
            sent → record message id ; unknown → status=unknown
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .agent import AgentAdapter, AgentResult
from .autonomy import AutonomousLoop
from .config import AppConfig
from .conversation import build_prompt, is_duplicate_of_history, parse_reply
from .policy import (
    Decision,
    ParticipationPolicy,
    PolicyConfig,
    TRIGGER_MENTION,
    TRIGGER_PROACTIVE,
    TRIGGER_REPLY,
    TurnContext,
    proactive_score,
)
from .store import SendRecord, Store, StoredMessage
from .telegram import (
    InboundMessage,
    InboundReaction,
    SendOutcome,
    SendStatus,
    TelegramClient,
    parse_reaction_update,
)
from .turns import Turn, TurnManager

log = logging.getLogger(__name__)

# After sending, remember the reply's message id so the local log matches what
# Telegram shows without needing a second round trip.
BOT_LABEL = "bot"


@dataclass
class TickStats:
    """One poll cycle, for diagnostics. Counts, never message text."""

    updates: int = 0
    duplicates: int = 0
    unauthorized: int = 0
    ignored: int = 0
    messages_stored: int = 0
    turns_closed: int = 0
    replies_decided: int = 0
    silences_decided: int = 0
    superseded: int = 0
    duplicate_replies: int = 0
    sends_ok: int = 0
    sends_failed: int = 0
    sends_unknown: int = 0
    model_calls: int = 0
    feedback_updates: int = 0
    errors: int = 0
    admin_commands: int = 0
    admin_refused: int = 0


@dataclass
class BotStats:
    started_at: float = field(default_factory=time.time)
    ticks: int = 0
    total: TickStats = field(default_factory=TickStats)
    last_tick: TickStats | None = None
    paused: bool = False
    proactive_enabled: bool = False

    def absorb(self, tick: TickStats) -> None:
        self.ticks += 1
        self.last_tick = tick
        for name in TickStats.__dataclass_fields__:
            setattr(self.total, name, getattr(self.total, name) + getattr(tick, name))

    def as_dict(self) -> dict[str, Any]:
        data = {
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "ticks": self.ticks,
            "paused": self.paused,
            "proactive_enabled": self.proactive_enabled,
        }
        data["total"] = dict(self.total.__dict__)
        if self.last_tick is not None:
            data["last_tick"] = dict(self.last_tick.__dict__)
        return data


class AgentFactory(Protocol):
    def __call__(self, spec: "AgentSpec") -> AgentAdapter: ...


@dataclass
class AgentSpec:
    """Everything the factory needs to build a per-chat agent."""

    chat_id: int
    workspace: Path
    system_prompt: str
    tool_catalog: Any = None
    response_commit_hook: Any = None
    session_id: str | None = None
    restore: bool = False


class GroupAgent:
    """Owns the store, the turn manager, the policy, the agents, and the client."""

    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        client: TelegramClient,
        system_prompt: str,
        agent_factory: AgentFactory,
        turn_manager: TurnManager | None = None,
        policy: ParticipationPolicy | None = None,
        bot_user_id: int | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
        autonomy: AutonomousLoop | None = None,
        legacy_policy: bool = False,
    ) -> None:
        self.config = config
        self.store = store
        # The store's clock is chosen at construction (`Store(path, clock=...)`)
        # rather than overwritten here: a caller may deliberately give storage
        # and scheduling different time sources, and silently swapping it would
        # break that. Pass the same clock to both when they must agree.
        self.client = client
        self.system_prompt = system_prompt
        self._agent_factory = agent_factory
        self.turns = turn_manager or TurnManager(
            debounce_seconds=config.debounce_seconds,
            max_messages=config.max_turn_messages,
        )
        self.policy = policy or ParticipationPolicy(
            PolicyConfig(
                proactive_enabled=config.proactive_enabled,
                min_interval_seconds=config.proactive_min_interval_seconds,
                max_per_hour=config.proactive_max_per_hour,
                # This was configured and read into AppConfig but never reached
                # the policy, so the knob did nothing.
                threshold=config.proactive_min_confidence,
            )
        )
        self.bot_user_id = bot_user_id
        self._clock = clock
        self._sleep = sleep
        # Keyed by (chat_id, topic_id): a forum topic gets its own runtime.
        self._agents: dict[tuple[int, int | None], AgentAdapter] = {}
        self._agent_failures: dict[tuple[int, int | None], str] = {}
        # Turns closed by the size cap during ingest, waiting for run_turns.
        self._pending_turns: list[Turn] = []
        # Set by an admin command; surfaced in diagnostics so a pause is visible.
        self._admin_note: str | None = None
        # How many inbound messages startup had to re-open. Diagnostic only.
        self.recovered_inbound = 0
        self.stats = BotStats(proactive_enabled=config.proactive_enabled)
        self._started = False
        self._recovered_sends: list[SendRecord] = []
        self._recovered_decisions: list[Any] = []
        self._topic_locks: dict[tuple[int, int | None], asyncio.Lock] = {}
        self._workers: set[asyncio.Task] = set()
        self._worker_limit = asyncio.Semaphore(4)
        # The default participation model: observations wake the agent, and
        # the agent decides what to do with the opportunity. The legacy
        # policy path is retained only for explicitly constructed callers and
        # for replaying old data, never as a fallback when the agent is unsure.
        self.autonomy: AutonomousLoop | None = None
        self.legacy_policy = legacy_policy
        if autonomy is None and not legacy_policy:
            self.autonomy = AutonomousLoop(
                store=self.store,
                agent_for=self._agent_for,
                system_prompt=self.system_prompt,
                clock=self._clock,
                idle_seconds=getattr(config, "idle_seconds", 2.0),
                max_batch_seconds=getattr(config, "max_batch_seconds", 15.0),
                max_batch_observations=getattr(config, "max_batch_observations", 24),
                send=self._send_action,
                bot_name=config.bot_name,
                bot_user_id=bot_user_id,
                max_activations_per_hour=getattr(config, "max_activations_per_hour", 60),
                max_actions_per_hour=getattr(config, "max_actions_per_hour", 20),
                min_action_interval_seconds=getattr(
                    config, "min_action_interval_seconds", 20.0),
            )
        else:
            self.autonomy = autonomy

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        # Startup recovery, in the order that matters:
        # 1. A 'sending' row outlived its process → UNKNOWN, never retried.
        orphaned = self.store.recover_orphaned_sends()
        if orphaned:
            log.warning(
                "marked %d orphaned send(s) as unknown (delivery unconfirmed): %s",
                len(orphaned), ", ".join(orphaned[:5]),
            )
        # 2. 'pending' rows are safe: the intent was recorded, the request was not.
        self._recovered_sends = self.store.recover_pending_sends()
        self._recovered_decisions = self.store.unfinished_decisions()

        if self.autonomy is not None:
            # Autonomous recovery is driven by the observation cursor, not by
            # turns: any observation past the watermark is replayed by the
            # next activation, and a 'sending' action is never retried.
            orphaned_actions = self.store.recover_orphaned_actions()
            if orphaned_actions:
                log.warning(
                    "marked %d interrupted action(s) as unknown delivery: %s",
                    len(orphaned_actions), ", ".join(orphaned_actions[:5]),
                )
            for row in self.store.running_workflows():
                # A workflow that never reached a terminal state did not move
                # the cursor, so its observations will be seen again. Mark the
                # stale row so the audit trail does not claim it finished.
                self.store.close_workflow(
                    str(row["workflow_id"]), "failed",
                    outcome="interrupted_by_restart",
                )
            self.recovered_inbound = self._count_pending_observations()
            if self.recovered_inbound:
                # Counting is not recovering. Without re-arming the batch for
                # every scope that still has unprocessed observations, those
                # messages sit past the cursor forever and only get looked at
                # if some later message happens to arrive in the same topic.
                for chat_id, topic_id in self.store.scopes_with_pending_observations():
                    if not self.config.allows_chat(chat_id):
                        continue
                    self.autonomy.note_activity(chat_id, topic_id)
                log.warning("queued %d observation(s) past the cursor for replay",
                            self.recovered_inbound)
            return

        revived = 0
        covered: set[tuple[int, int]] = set()
        for row in self.store.recover_open_turns():
            if not self.config.allows_chat(row["chat_id"]):
                continue
            messages = [m for m in self.store.messages_for_turn(row) if not self._is_admin_command(m)]
            if not messages:
                continue
            covered.update((m.chat_id, m.message_id) for m in messages)
            self._pending_turns.append(Turn(
                turn_id=row["turn_id"], chat_id=row["chat_id"], topic_id=row["topic_id"],
                messages=messages, opened_at=0, closed_at=0))
            revived += len(messages)
        for message in self.store.recover_unanswered_messages(limit=1000000):
            if (message.chat_id, message.message_id) in covered:
                continue
            if not self.config.allows_chat(message.chat_id):
                continue
            if self._is_admin_command(message):
                continue
            if message.is_bot and message.sender_id == self.bot_user_id:
                continue
            full = self.turns.observe(message)
            if full is not None:
                self._pending_turns.append(full)
            revived += 1
        self.recovered_inbound = revived
        if revived:
            log.warning(
                "recovered %d ingested message(s) with no decision; "
                "they will be decided on the next turn pass", revived,
            )

    async def stop(self) -> None:
        for task in list(self._workers):
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        for (chat_id, topic_id), agent in list(self._agents.items()):
            try:
                await agent.stop()
            except Exception as exc:
                log.warning("agent for chat %s topic %s failed to stop: %s",
                            chat_id, topic_id, exc)
        self._agents.clear()
        self._started = False

    # -- one poll cycle ----------------------------------------------------

    async def poll_once(self, *, offset: int | None, background: bool = False) -> tuple[int | None, TickStats]:
        """Fetch one batch, ingest it durably, then close and answer turns."""
        tick = TickStats()
        try:
            updates = await self.client.get_updates(
                offset=offset,
                timeout=self.config.poll_timeout,
            )
        except Exception as exc:
            log.warning("getUpdates failed: %s", exc)
            tick.errors += 1
            self.stats.absorb(tick)
            return offset, tick

        tick.updates = len(updates)
        highest: int | None = None
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                highest = update_id if highest is None else max(highest, update_id)
            reaction = parse_reaction_update(update)
            if reaction is not None:
                self._ingest_reaction(reaction, tick)
                continue
            self._ingest(update, tick)

        # Advance the cursor only after durable ingestion of this batch.
        new_offset = offset
        if highest is not None:
            new_offset = highest + 1
            self.store.set_update_cursor(new_offset)

        if background:
            self.schedule_turns()
        else:
            await self.run_turns(tick)
        self.stats.absorb(tick)
        return new_offset, tick

    # A shared tick for the whole `step()` call, so ingest and turn counters land
    # in the same record instead of one being discarded.
    async def step(self, updates: list[dict[str, Any]], *,
                   run: bool | None = None) -> tuple[TickStats, list[Decision]]:
        """Ingest a batch, then close and answer turns. The unit tests' entry point.

        Same ordering as `poll_once` minus the network fetch, so a test exercises
        the production sequence instead of a parallel one.

        `run=False` ingests only, matching `poll_once(background=True)` in live
        mode where a separate wake loop drives activations. Tests that want to
        control when the model is consulted need that separation; without it a
        call to `step()` silently performs an activation and any later explicit
        pass has nothing left to do.
        """
        tick = TickStats()
        tick.updates = len(updates)
        for update in updates:
            reaction = parse_reaction_update(update)
            if reaction is not None:
                self._ingest_reaction(reaction, tick)
                continue
            self._ingest(update, tick)
        # In the legacy path a turn is decided synchronously, so running is
        # what callers expect. The autonomous path is driven by a scheduler:
        # letting step() also activate would hide when the model is consulted
        # and make an explicit pass afterwards a no-op. Default accordingly,
        # and let a caller override either way.
        should_run = (self.autonomy is None) if run is None else run
        decisions = await self.run_turns(tick) if should_run else []
        self.stats.absorb(tick)
        return tick, decisions

    async def autonomous_step(self, *, force: bool = True) -> list[Any]:
        """One autonomous pass, for tests that drive the clock directly."""
        if self.autonomy is None:
            return []
        return await self.autonomy.tick(force=force)

    # -- ingest ------------------------------------------------------------

    def _ingest_reaction(self, reaction: InboundReaction, tick: TickStats) -> None:
        if not self.config.allows_chat(reaction.chat_id):
            tick.unauthorized += 1
            return
        result = self.store.record_reaction_feedback(
            update_id=reaction.update_id,
            chat_id=reaction.chat_id,
            message_id=reaction.message_id,
            old_vote=reaction.old_vote,
            new_vote=reaction.new_vote,
        )
        if result == "recorded":
            tick.feedback_updates += 1
        elif result == "duplicate":
            tick.duplicates += 1
        else:
            tick.ignored += 1

    def _ingest(self, update: dict[str, Any], tick: TickStats) -> None:
        inbound = _parse(update)
        if inbound is None:
            tick.ignored += 1
            return

        if not self.config.allows_chat(inbound.chat_id):
            # Drop without touching the store: an unauthorized chat must not
            # create a row, a log entry, or a turn.
            tick.unauthorized += 1
            log.debug("ignored update from unauthorized chat %s", inbound.chat_id)
            return

        message = StoredMessage(
            chat_id=inbound.chat_id,
            message_id=inbound.message_id,
            topic_id=inbound.topic_id,
            sender_id=inbound.sender_id,
            sender_label=inbound.sender_label,
            is_bot=inbound.is_bot,
            text=inbound.text,
            created_at=inbound.created_at or self._clock(),
            reply_to_bot=(
                self.bot_user_id is not None
                and inbound.reply_to_sender_id == self.bot_user_id
            ),
            media_kind=inbound.media_kind,
            media_hint=inbound.media_hint,
        )
        if not self.store.record_inbound_message(
            message, update_id=inbound.update_id, payload=_minimal_payload(inbound)
        ):
            # Same (chat, message) under a new update id: an edit, most likely.
            tick.duplicates += 1
            return

        tick.messages_stored += 1

        # Our own message echoed back by the API: keep it in the log for context,
        # but never open a turn on it.
        if inbound.is_bot and inbound.sender_id == self.bot_user_id:
            return

        # Admin commands are handled here and never become model input.
        if self._handle_admin(message, tick):
            return

        if self.autonomy is not None:
            # The observation is already durable. Scheduling decides *when*
            # the agent gets to look; it never decides whether it may think.
            self.autonomy.note_activity(message.chat_id, message.topic_id)
            return

        full = self.turns.observe(message)
        if full is not None:
            tick.turns_closed += 1
            # Turns closed by the size cap are processed by run_turns via the
            # queue below; ready() only returns time-closed turns.
            self._pending_turns.append(full)

    # -- turn processing ---------------------------------------------------

    def _schedule(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)

    def schedule_turns(self) -> None:
        """Schedule each ready topic independently of polling and other topics."""
        if self.autonomy is not None:
            # `run_turns()` is the autonomous pass; it must not also run the
            # legacy turn path or there would be two participation models.
            self._schedule(self._autonomous_pass())
            return
        for work in self._take_recovery(TickStats()):
            self._schedule(work)
        closed = list(self._pending_turns)
        self._pending_turns.clear()
        closed.extend(self.turns.ready())
        closed.sort(key=lambda turn: turn.opened_at)
        for turn in closed:
            self._schedule(self._scheduled_turn(turn))

    async def _scheduled_turn(self, turn: Turn) -> None:
        tick = TickStats()
        try:
            decision = await self._run_one(turn, tick)
            tick.turns_closed += 1
            if decision.is_reply:
                tick.replies_decided += 1
            else:
                tick.silences_decided += 1
        except Exception:
            tick.errors += 1
            log.exception("turn %s failed", turn.turn_id)
            self._record_failed_turn(turn, tick)
        finally:
            self.stats.absorb(tick)

    async def _run_one(self, turn: Turn, tick: TickStats) -> Decision:
        key = (turn.chat_id, turn.topic_id)
        lock = self._topic_locks.setdefault(key, asyncio.Lock())
        async with lock:
            async with self._worker_limit:
                return await self._process_turn(turn, tick)

    def _take_recovery(self, tick: TickStats) -> list[Any]:
        sends, self._recovered_sends = self._recovered_sends, []
        decisions, self._recovered_decisions = self._recovered_decisions, []
        work = []
        for record in sends:
            rows = self.store._query(
                "SELECT t.opened_at FROM decisions d JOIN turns t ON t.turn_id = d.turn_id"
                " WHERE d.decision_id = ?", (record.decision_id,))
            work.append((rows[0]["opened_at"] if rows else 0, self._recover_send(record, tick)))
        work.extend((row["opened_at"], self._recover_decision(row, tick)) for row in decisions)
        return [coroutine for _, coroutine in sorted(work, key=lambda item: item[0])]

    async def _recover_send(self, record: SendRecord, tick: TickStats) -> None:
        async with self._topic_locks.setdefault((record.chat_id, record.topic_id), asyncio.Lock()):
            async with self._worker_limit:
                if not self.config.allows_chat(record.chat_id) or self.store.pending_obsolete(record.decision_id):
                    self.store.cancel_pending(record.decision_id, "obsolete or unauthorized")
                    return
                rows = self.store._query("SELECT turn_id FROM decisions WHERE decision_id = ?", (record.decision_id,))
                if not rows:
                    self.store.cancel_pending(record.decision_id, "missing decision")
                    return
                turn = Turn(rows[0]["turn_id"], record.chat_id, record.topic_id, [], 0, 0)
                await self._deliver(decision_id=record.decision_id, chat_id=record.chat_id,
                                    topic_id=record.topic_id, reply_to=record.reply_to,
                                    text=record.text, turn=turn, tick=tick)

    async def _recover_decision(self, row: Any, tick: TickStats) -> None:
        async with self._topic_locks.setdefault((row["chat_id"], row["topic_id"]), asyncio.Lock()):
            async with self._worker_limit:
                if not self.config.allows_chat(row["chat_id"]):
                    return
                if time.time() - min(row["created_at"], row["opened_at"]) > 86400:
                    self.store.close_turn(row["turn_id"], "expired")
                    return
                messages = [m for m in self.store.messages_for_turn(row) if not self._is_admin_command(m)]
                if not messages:
                    return
                turn = Turn(row["turn_id"], row["chat_id"], row["topic_id"], messages, 0, 0)
                import json
                decision = Decision(row["outcome"], row["reason"], row["trigger"], detail=json.loads(row["detail_json"]))
                history = self.store.recent_messages(turn.chat_id, topic_id=turn.topic_id, limit=self.config.history_messages)
                await self._reply(turn, decision, row["decision_id"], history, tick)

    async def _recover_work(self, tick: TickStats) -> None:
        await asyncio.gather(*self._take_recovery(tick))

    def _session_id_for(self, chat_id: int, topic_id: int | None) -> str:
        """A stable per-scope session id, derived rather than random.

        The journal stores the transcript under this id, so it must not change
        between restarts. Scope is included because one sqlite journal can
        serve several chats; sharing an id would leak one group's history into
        another's model view.
        """
        scope = f"chat_{chat_id}" if topic_id is None else f"chat_{chat_id}_topic_{topic_id}"
        return f"tg_{scope}"

    def _tool_catalog_for(self) -> Any:
        """One catalog per agent; the handler is rebound per workflow."""
        if self.autonomy is None:
            return None
        from .autonomous_tools import AutonomousToolCatalog

        return AutonomousToolCatalog()

    def _response_hook_for(self, chat_id: int, topic_id: int | None) -> Any:
        """A hook bound to one scope, so concurrent topics cannot cross-talk.

        The runtime calls it after response validation and before tool
        dispatch. Raising here prevents every tool effect: an intent that
        cannot be recorded must not happen.
        """
        if self.autonomy is None:
            return None

        async def hook(activation: Any, response: Any) -> None:
            self.store.commit_autonomous_response(chat_id, topic_id, activation, response)

        return hook

    async def _send_action(self, *, chat_id: int, topic_id: int | None, text: str,
                           reply_to: int | None) -> Any:
        """Transport boundary for an already-claimed action intent."""
        return await self.client.send_message(
            chat_id=chat_id, text=text, reply_to_message_id=reply_to, topic_id=topic_id,
        )

    def _count_pending_observations(self) -> int:
        """Unprocessed observations across all known scopes, for diagnostics."""
        rows = self.store._query(
            "SELECT o.chat_id, o.topic_id, COUNT(*) AS n FROM observation_events o"
            " LEFT JOIN observation_cursors c"
            "   ON c.chat_id = o.chat_id AND c.topic_id IS o.topic_id"
            " WHERE o.sequence > COALESCE(c.last_processed, 0)"
            " GROUP BY o.chat_id, o.topic_id"
        )
        return sum(int(row["n"]) for row in rows)

    def queued_topics(self) -> list[tuple[int, int | None]]:
        return sorted(self.autonomy._batch_started) if self.autonomy else []

    async def _autonomous_pass(self) -> None:
        assert self.autonomy is not None
        async with self._worker_limit:
            try:
                await self.autonomy.tick()
            except Exception:
                log.exception("autonomous pass failed")

    async def run_turns(self, tick: TickStats | None = None) -> list[Decision]:
        if self.autonomy is not None:
            await self._autonomous_pass()
            return []
        """Process every closed turn.

        Counters go into the caller's `tick` when one is supplied (so `step()` and
        `poll_once()` keep ingest and turn counts in one record); otherwise they go
        into a fresh one that is absorbed immediately.
        """
        owns_tick = tick is None
        if tick is None:
            tick = TickStats()
        try:
            await self._recover_work(tick)
            closed = list(self._pending_turns)
            self._pending_turns.clear()
            closed.extend(self.turns.ready())
            closed.sort(key=lambda turn: turn.opened_at)

            decisions: list[Decision] = []
            for turn in closed:
                # One bad turn must not take the batch with it. A failure is
                # recorded on that turn, the rest still get decided, and the
                # caller never sees an exception that would stop the poll loop.
                try:
                    decision = await self._run_one(turn, tick)
                except Exception:
                    tick.errors += 1
                    log.exception(
                        "turn %s failed (chat=%s topic=%s); continuing with the rest",
                        turn.turn_id, turn.chat_id, turn.topic_id,
                    )
                    self._record_failed_turn(turn, tick)
                    continue
                decisions.append(decision)
                tick.turns_closed += 1
                if decision.is_reply:
                    tick.replies_decided += 1
                else:
                    tick.silences_decided += 1
            return decisions
        finally:
            if owns_tick:
                self.stats.absorb(tick)

    def _record_failed_turn(self, turn: Turn, tick: TickStats) -> None:
        """Persist a turn that raised, so it is visible and not silently lost.

        `open_turn` writes the window's high-water mark, which is what stops the
        same batch being answered twice after a restart. It is written here too:
        if the failure happened after the claim, re-opening it on restart would
        duplicate whatever did get sent.
        """
        try:
            self.store.open_turn(
                turn_id=turn.turn_id, chat_id=turn.chat_id, topic_id=turn.topic_id,
                first_message=turn.first_message_id, last_message=turn.last_message_id,
                trigger="error",
            )
            self.store.record_decision(
                decision_id=f"dec_{uuid.uuid4().hex[:12]}",
                chat_id=turn.chat_id, topic_id=turn.topic_id, turn_id=turn.turn_id,
                outcome="silent", reason="turn_failed",
                detail={"messages": len(turn.messages), "error": True},
                trigger=TRIGGER_PROACTIVE,
            )
            self.store.close_turn(turn.turn_id, "error")
        except Exception:
            log.exception("could not record the failed turn %s", turn.turn_id)

    async def _process_turn(self, turn: Turn, tick: TickStats) -> Decision:
        decision_id = f"dec_{uuid.uuid4().hex[:12]}"
        history = self.store.recent_messages(
            turn.chat_id, topic_id=turn.topic_id, limit=self.config.history_messages
        )
        # A paused bot still ingests (see main.run_live) but never replies.
        if self.stats.paused:
            decision = Decision.silent(
                reason="operator_paused", trigger=TRIGGER_PROACTIVE,
                confidence=0.0, detail={"messages": len(turn.messages)},
            )
            self.store.open_turn(
                turn_id=turn.turn_id, chat_id=turn.chat_id, topic_id=turn.topic_id,
                first_message=turn.first_message_id, last_message=turn.last_message_id,
                trigger=decision.trigger,
            )
            self.store.record_decision(
                decision_id=decision_id, chat_id=turn.chat_id, topic_id=turn.topic_id,
                turn_id=turn.turn_id, outcome=decision.outcome,
                reason=decision.reason, detail=decision.detail, trigger=decision.trigger,
            )
            self.store.close_turn(turn.turn_id, "silent")
            log.info("silent chat=%s turn=%s reason=operator_paused",
                     turn.chat_id, turn.turn_id)
            return decision

        context = self._context(turn, history)
        decision = self.policy.decide(context)

        # Claim the window BEFORE deciding, so a crash between here and the send
        # cannot make the same batch of messages the subject of a second turn on
        # the next start. `last_message` is the recovery high-water mark; leaving
        # it unwritten until after the model call would turn every crash into a
        # duplicate answer.
        self.store.open_turn(
            turn_id=turn.turn_id,
            chat_id=turn.chat_id,
            topic_id=turn.topic_id,
            first_message=turn.first_message_id,
            last_message=turn.last_message_id,
            trigger=decision.trigger,
        )
        self.store.record_decision(
            decision_id=decision_id,
            chat_id=turn.chat_id,
            topic_id=turn.topic_id,
            turn_id=turn.turn_id,
            outcome=decision.outcome,
            reason=decision.reason,
            detail=decision.detail,
            trigger=decision.trigger,
        )

        if decision.is_silent:
            self.store.close_turn(turn.turn_id, "silent")
            log.info(
                "silent chat=%s turn=%s reason=%s detail=%s",
                turn.chat_id, turn.turn_id, decision.reason, decision.detail,
            )
            return decision

        await self._reply(turn, decision, decision_id, history, tick)
        return decision

    async def _reply(
        self,
        turn: Turn,
        decision: Decision,
        decision_id: str,
        history: Sequence[StoredMessage],
        tick: TickStats,
    ) -> None:
        agent = await self._agent_for(turn.chat_id, turn.topic_id)
        if agent is None:
            self.store.close_turn(turn.turn_id, "silent")
            log.warning("no agent available for chat %s topic %s",
                        turn.chat_id, turn.topic_id)
            return

        prompt = build_prompt(
            chat_id=turn.chat_id,
            topic_id=turn.topic_id,
            history=[m for m in history if not self._is_admin_command(m)],
            turn_messages=[m for m in turn.messages if not self._is_admin_command(m)],
            bot_user_id=self.bot_user_id,
            bot_name=self.config.bot_name,
        )

        # Supersede older reply decisions for this chat that never got sent.
        for row in self.store.recent_decisions(turn.chat_id, limit=5):
            if (
                row["decision_id"] != decision_id
                and row["topic_id"] == turn.topic_id
                and row["outcome"] == "reply"
                and row["superseded_by"] is None
            ):
                send = self.store.get_send(row["decision_id"])
                if send is None or send.status == "pending":
                    if self.store.supersede(row["decision_id"], decision_id):
                        tick.superseded += 1

        tick.model_calls += 1
        started = self._clock()
        try:
            result = await agent.ask(chat_id=turn.chat_id, prompt=prompt.text, tag=decision_id)
        except Exception as exc:
            tick.errors += 1
            log.exception("agent call failed for chat %s", turn.chat_id)
            self.store.close_turn(turn.turn_id, "error")
            return
        latency_ms = int((self._clock() - started) * 1000)

        reply = parse_reply(result.text or "", bot_name=self.config.bot_name)
        detail = {
            **decision.detail,
            "decision_id": decision_id,
            "latency_ms": latency_ms,
            "activation": result.detail,
            "prompt_chars": len(prompt.text),
            "prompt_messages": prompt.included_messages,
        }
        # The activation's usage is only known now. Persist it onto the reply
        # decision, which was written before the model call: otherwise successful
        # replies have no token/cache accounting on record.
        self.store.update_decision_detail(decision_id, detail)

        if result.status in {"error", "timeout", "rejected", "paused"}:
            # An adapter/provider failure is not an intentional empty answer.
            # Keep the failed attempt visible in diagnostics and error metrics.
            tick.errors += 1
            self.store.record_decision(
                decision_id=decision_id + "_error",
                chat_id=turn.chat_id, topic_id=turn.topic_id, turn_id=turn.turn_id,
                outcome="silent", reason="model_activation_failed",
                detail=detail, trigger=decision.trigger,
            )
            self.store.close_turn(turn.turn_id, "error")
            log.warning("agent activation failed: %s", result.detail)
            return

        if reply is None:
            # The model produced nothing usable. This is not a silent decision;
            # it is a failed attempt, and it is recorded as such.
            self.store.record_decision(
                decision_id=decision_id + "_empty",
                chat_id=turn.chat_id, topic_id=turn.topic_id, turn_id=turn.turn_id,
                outcome="silent", reason="model_returned_nothing",
                detail=detail, trigger=decision.trigger,
            )
            self.store.close_turn(turn.turn_id, "empty")
            log.warning("activation produced nothing sendable: %s", result.detail)
            return

        # Superseded while we were waiting for the model?
        if self.store.is_superseded(decision_id):
            tick.superseded += 1
            self.store.close_turn(turn.turn_id, "superseded")
            log.info("dropped superseded reply for chat %s", turn.chat_id)
            return

        if is_duplicate_of_history(reply.text, history):
            tick.duplicate_replies += 1
            self.store.close_turn(turn.turn_id, "duplicate")
            log.info("dropped duplicate reply for chat %s", turn.chat_id)
            return

        newer_rows = self.store._query(
            "SELECT * FROM messages WHERE chat_id = ? AND topic_id IS ? AND is_bot = 0"
            " AND message_id > ? ORDER BY message_id",
            (turn.chat_id, turn.topic_id, turn.last_message_id),
        )
        if newer_rows:
            from .store import _row_to_message
            newer = [_row_to_message(row) for row in newer_rows]
            turn.messages.extend(newer)
            with self.store._write() as conn:
                conn.execute("UPDATE turns SET last_message = ? WHERE turn_id = ?",
                             (turn.last_message_id, turn.turn_id))
            history = self.store.recent_messages(turn.chat_id, topic_id=turn.topic_id,
                                                 limit=self.config.history_messages)
            await self._reply(turn, decision, decision_id, history, tick)
            return

        self.store.prepare_send(
            decision_id=decision_id,
            chat_id=turn.chat_id,
            topic_id=turn.topic_id,
            reply_to=turn.last_message_id,
            text=reply.text,
        )
        await self._deliver(
            decision_id=decision_id,
            chat_id=turn.chat_id,
            topic_id=turn.topic_id,
            reply_to=turn.last_message_id,
            text=reply.text,
            turn=turn,
            tick=tick,
        )

    async def _deliver(
        self,
        *,
        decision_id: str,
        chat_id: int,
        topic_id: int | None,
        reply_to: int | None,
        text: str,
        turn: Turn,
        tick: TickStats,
    ) -> None:
        """Claim, send, record. The claim is the last durable step before I/O."""
        if not self.store.mark_sending(decision_id):
            log.warning("send %s was not claimable; skipping", decision_id)
            self.store.cancel_pending(decision_id, "obsolete or not claimable")
            return

        outcome: SendOutcome = await self.client.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to,
            topic_id=topic_id,
        )
        if outcome.status is SendStatus.SENT:
            tick.sends_ok += 1
            message = None
            if outcome.telegram_message_id and outcome.telegram_message_id > 0:
                message = StoredMessage(chat_id=chat_id, message_id=outcome.telegram_message_id,
                                        topic_id=topic_id, sender_id=self.bot_user_id,
                                        sender_label=self.config.bot_name, is_bot=True,
                                        text=text, created_at=self._clock())
            self.store.finish_sent(decision_id, outcome.telegram_message_id or 0, message)
        elif outcome.status is SendStatus.UNKNOWN:
            tick.sends_unknown += 1
            self.store.mark_send_unknown(decision_id, outcome.error or "unknown")
            self.store.close_turn(turn.turn_id, "unknown")
            log.warning(
                "send %s has unknown delivery for chat %s: %s",
                decision_id, chat_id, outcome.error,
            )
        else:
            tick.sends_failed += 1
            self.store.mark_send_failed(decision_id, outcome.error or "rejected")
            self.store.close_turn(turn.turn_id, "failed")
            log.warning("send %s rejected for chat %s: %s", decision_id, chat_id, outcome.error)

    # -- context and agents ------------------------------------------------

    def _context(self, turn: Turn, history: Sequence[StoredMessage]) -> TurnContext:
        now = self._clock()
        last_bot_at = None
        last_bot_text = None
        for message in reversed(history):
            if message.is_bot or (
                self.bot_user_id is not None and message.sender_id == self.bot_user_id
            ):
                last_bot_at = message.created_at
                last_bot_text = message.text
                break

        hour_ago = now - 3600
        replies_last_hour = 0
        for row in self.store.recent_decisions(turn.chat_id, limit=50):
            if row["outcome"] != "reply":
                continue
            created = float(row["created_at"])
            if created < hour_ago:
                break
            send = self.store.get_send(row["decision_id"])
            if send is not None and send.status in {"sent", "unknown"}:
                replies_last_hour += 1

        return TurnContext(
            turn=turn,
            recent_messages=history,
            bot_user_id=self.bot_user_id,
            bot_name=self.config.bot_name,
            bot_username=self.config.bot_username,
            last_bot_message_at=last_bot_at,
            last_bot_reply_text=last_bot_text,
            replies_last_hour=replies_last_hour,
            now=now,
        )

    async def _agent_for(self, chat_id: int, topic_id: int | None) -> AgentAdapter | None:
        """One agent per (chat, topic).

        Keying by chat alone would let two forum topics share one `Transcript`,
        which is the runtime's model view and is append-only for the life of a
        Runtime. A live simulation caught exactly that: asked in topic B "what did
        we just say about the timeout", the bot answered from topic A's history.
        The application's store already scopes by topic; the agent must match it.
        """
        key = (chat_id, topic_id)
        existing = self._agents.get(key)
        if existing is not None:
            return existing
        suffix = f"chat_{chat_id}" if topic_id is None else f"chat_{chat_id}_topic_{topic_id}"
        spec = AgentSpec(
            chat_id=chat_id,
            workspace=Path(self.config.workspace) / suffix,
            system_prompt=self.system_prompt,
            tool_catalog=self._tool_catalog_for(),
            response_commit_hook=self._response_hook_for(chat_id, topic_id),
            session_id=self._session_id_for(chat_id, topic_id),
            restore=self.autonomy is not None,
        )
        try:
            agent = self._agent_factory(spec)
            await agent.start()
        except Exception as exc:
            self._agent_failures[key] = str(exc)
            log.exception("could not start an agent for chat %s topic %s",
                          chat_id, topic_id)
            return None
        self._agents[key] = agent
        return agent

    # -- admin -------------------------------------------------------------

    ADMIN_COMMANDS = ("pause", "resume", "proactive", "stats", "purge", "help")

    def hold_open_turns(self) -> int:
        """Close every open debounce window so shutdown can answer them.

        The caller then runs `run_turns()`. Returns how many windows were closed.
        """
        closed = self.turns.flush()
        self._pending_turns.extend(closed)
        return len(closed)

    @classmethod
    def _is_admin_command(cls, message: StoredMessage) -> bool:
        text = message.text.strip()
        return bool(text.startswith("/") and text.split(maxsplit=1)[0].lstrip("/").split("@")[0].lower() in cls.ADMIN_COMMANDS)

    def _handle_admin(self, message: StoredMessage, tick: TickStats) -> bool:
        """Handle an admin command. Returns True if the message was consumed.

        Only a configured admin, only in an authorized chat, and only a bare
        `/command`. The message is never forwarded to the model, and a refused
        command is dropped rather than answered — replying "you are not an
        admin" in a public group would leak the admin list's existence.
        """
        text = message.text.strip()
        if not text.startswith("/"):
            return False
        head = text.split(maxsplit=1)[0].lstrip("/").split("@")[0].lower()
        if head not in self.ADMIN_COMMANDS:
            return False
        if not self.config.is_admin(message.sender_id):
            tick.admin_refused += 1
            log.warning("refused admin command %r from non-admin %s",
                        head, message.sender_id)
            return True

        argument = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) > 1 else ""
        if head == "pause":
            self.pause()
            self._admin_note = "paused"
        elif head == "resume":
            self.resume()
            self._admin_note = "resumed"
        elif head == "proactive":
            enabled = argument in {"on", "1", "true", "yes"}
            self.set_proactive(enabled)
            self._admin_note = f"proactive {'on' if enabled else 'off'}"
        elif head == "purge":
            self.purge(message.chat_id)
            self._admin_note = "purged this chat"
        elif head == "stats":
            self._admin_note = None
        elif head == "help":
            self._admin_note = None
        tick.admin_commands += 1
        log.info("admin command %r applied for chat %s", head, message.chat_id)
        return True

    def pause(self) -> None:
        self.stats.paused = True

    def resume(self) -> None:
        self.stats.paused = False

    def set_proactive(self, enabled: bool) -> None:
        self.stats.proactive_enabled = enabled
        self.policy.config.proactive_enabled = enabled

    def purge(self, chat_id: int) -> None:
        self.store.purge_chat(chat_id)

    def diagnostics(self) -> dict[str, Any]:
        data = self.stats.as_dict()
        data["config"] = self.config.describe()
        data["store"] = self.store.stats()
        data["agents"] = {
            f"{chat_id}" if topic_id is None else f"{chat_id}/topic_{topic_id}": {
                "failed": self._agent_failures.get((chat_id, topic_id)),
                "stats": getattr(agent, "stats", lambda: {})() if hasattr(agent, "stats") else {},
            }
            for (chat_id, topic_id), agent in self._agents.items()
        }
        data["open_turns"] = [list(key) for key in self.turns.open_keys()]
        data["last_admin_action"] = self._admin_note
        data["recovered_inbound"] = self.recovered_inbound
        return data


def _parse(update: dict[str, Any]) -> InboundMessage | None:
    from .telegram import parse_update

    return parse_update(update)


def _minimal_payload(inbound: InboundMessage) -> dict[str, Any]:
    """What we persist about an update.

    Deliberately not the raw Telegram JSON: it contains the sender's full name,
    username, and every service field. We keep identity we need to answer, and
    nothing else. `raw` stays in memory only for the duration of one ingest.
    """
    return {
        "update_id": inbound.update_id,
        "chat_id": inbound.chat_id,
        "message_id": inbound.message_id,
        "topic_id": inbound.topic_id,
        "sender_id": inbound.sender_id,
        "sender_label": inbound.sender_label,
        "is_bot": inbound.is_bot,
        "reply_to_message_id": inbound.reply_to_message_id,
        "date": inbound.created_at,
        "len": len(inbound.text),
    }
