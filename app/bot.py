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
from .telegram import InboundMessage, SendOutcome, SendStatus, TelegramClient
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
    ) -> None:
        self.config = config
        self.store = store
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
            )
        )
        self.bot_user_id = bot_user_id
        self._clock = clock
        self._sleep = sleep
        self._agents: dict[int, AgentAdapter] = {}
        self._agent_failures: dict[int, str] = {}
        # Turns closed by the size cap during ingest, waiting for run_turns.
        self._pending_turns: list[Turn] = []
        # Set by an admin command; surfaced in diagnostics so a pause is visible.
        self._admin_note: str | None = None
        # How many inbound messages startup had to re-open. Diagnostic only.
        self.recovered_inbound = 0
        self.stats = BotStats(proactive_enabled=config.proactive_enabled)
        self._started = False

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
        pending = self.store.recover_pending_sends()
        for record in pending:
            log.info("resuming prepared send %s for chat %s", record.decision_id, record.chat_id)

        # 3. Messages ingested but never turned into a decision (crash between
        #    the durable write and turn processing). Re-open them so the next
        #    run_turns() decides, instead of losing the message silently.
        revived = 0
        for message in self.store.unanswered_messages():
            # Recovery must apply the same authorization gate as live ingest,
            # otherwise a row written for a chat that was later revoked (or that
            # predates the allow-list) becomes a way around it.
            if not self.config.allows_chat(message.chat_id):
                log.warning(
                    "skipping recovered message %s from unauthorized chat %s",
                    message.message_id, message.chat_id,
                )
                continue
            if message.is_bot and message.sender_id == self.bot_user_id:
                continue
            self.turns.observe(message)
            revived += 1
        self.recovered_inbound = revived
        if revived:
            log.warning(
                "recovered %d ingested message(s) with no decision; "
                "they will be decided on the next turn pass", revived,
            )

    async def stop(self) -> None:
        for chat_id, agent in list(self._agents.items()):
            try:
                await agent.stop()
            except Exception as exc:
                log.warning("agent for chat %s failed to stop: %s", chat_id, exc)
        self._agents.clear()
        self._started = False

    # -- one poll cycle ----------------------------------------------------

    async def poll_once(self, *, offset: int | None) -> tuple[int | None, TickStats]:
        """Fetch one batch, ingest it durably, then close and answer turns."""
        tick = TickStats()
        try:
            updates = await asyncio.to_thread(
                self.client.get_updates,
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
            self._ingest(update, tick)

        # Advance the cursor only after durable ingestion of this batch.
        new_offset = offset
        if highest is not None:
            new_offset = highest + 1
            self.store.set_update_cursor(new_offset)

        await self.run_turns(tick)
        self.stats.absorb(tick)
        return new_offset, tick

    # A shared tick for the whole `step()` call, so ingest and turn counters land
    # in the same record instead of one being discarded.
    async def step(self, updates: list[dict[str, Any]]) -> tuple[TickStats, list[Decision]]:
        """Ingest a batch, then close and answer turns. The unit tests' entry point.

        Same ordering as `poll_once` minus the network fetch, so a test exercises
        the production sequence instead of a parallel one.
        """
        tick = TickStats()
        tick.updates = len(updates)
        for update in updates:
            self._ingest(update, tick)
        decisions = await self.run_turns(tick)
        self.stats.absorb(tick)
        return tick, decisions

    # -- ingest ------------------------------------------------------------

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

        # Dedupe BEFORE anything durable, so a redelivery costs one indexed read.
        inserted = self.store.record_update(
            update_id=inbound.update_id,
            chat_id=inbound.chat_id,
            message_id=inbound.message_id,
            payload=_minimal_payload(inbound),
        )
        if not inserted:
            tick.duplicates += 1
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
        )
        if not self.store.record_message(message, update_id=inbound.update_id):
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

        full = self.turns.observe(message)
        if full is not None:
            tick.turns_closed += 1
            # Turns closed by the size cap are processed by run_turns via the
            # queue below; ready() only returns time-closed turns.
            self._pending_turns.append(full)

    # -- turn processing ---------------------------------------------------

    async def run_turns(self, tick: TickStats | None = None) -> list[Decision]:
        """Process every closed turn.

        Counters go into the caller's `tick` when one is supplied (so `step()` and
        `poll_once()` keep ingest and turn counts in one record); otherwise they go
        into a fresh one that is absorbed immediately.
        """
        owns_tick = tick is None
        if tick is None:
            tick = TickStats()
        try:
            closed = list(self._pending_turns)
            self._pending_turns.clear()
            closed.extend(self.turns.ready(now=self._clock()))
            closed.sort(key=lambda turn: turn.opened_at)

            decisions: list[Decision] = []
            for turn in closed:
                decision = await self._process_turn(turn, tick)
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
        agent = await self._agent_for(turn.chat_id)
        if agent is None:
            self.store.close_turn(turn.turn_id, "silent")
            log.warning("no agent available for chat %s", turn.chat_id)
            return

        prompt = build_prompt(
            chat_id=turn.chat_id,
            topic_id=turn.topic_id,
            history=history,
            turn_messages=turn.messages,
            bot_user_id=self.bot_user_id,
            bot_name=self.config.bot_name,
        )

        # Supersede older reply decisions for this chat that never got sent.
        for row in self.store.recent_decisions(turn.chat_id, limit=5):
            if (
                row["decision_id"] != decision_id
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
            self.store.close_turn(turn.turn_id, "not_claimed")
            return

        outcome: SendOutcome = await asyncio.to_thread(
            self.client.send_message,
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to,
            topic_id=topic_id,
        )

        if outcome.status is SendStatus.SENT:
            tick.sends_ok += 1
            self.store.mark_sent(decision_id, outcome.telegram_message_id or 0)
            self.store.close_turn(turn.turn_id, "sent")
            if outcome.telegram_message_id and outcome.telegram_message_id > 0:
                self.store.record_message(
                    StoredMessage(
                        chat_id=chat_id,
                        message_id=outcome.telegram_message_id,
                        topic_id=topic_id,
                        sender_id=self.bot_user_id,
                        sender_label=self.config.bot_name,
                        is_bot=True,
                        text=text,
                        created_at=self._clock(),
                    ),
                    update_id=-1,
                )
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
            last_bot_message_at=last_bot_at,
            last_bot_reply_text=last_bot_text,
            replies_last_hour=replies_last_hour,
            now=now,
        )

    async def _agent_for(self, chat_id: int) -> AgentAdapter | None:
        existing = self._agents.get(chat_id)
        if existing is not None:
            return existing
        spec = AgentSpec(
            chat_id=chat_id,
            workspace=Path(self.config.workspace) / f"chat_{chat_id}",
            system_prompt=self.system_prompt,
        )
        try:
            agent = self._agent_factory(spec)
            await agent.start()
        except Exception as exc:
            self._agent_failures[chat_id] = str(exc)
            log.exception("could not start an agent for chat %s", chat_id)
            return None
        self._agents[chat_id] = agent
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
            str(chat_id): {
                "failed": self._agent_failures.get(chat_id),
                "stats": getattr(agent, "stats", lambda: {})() if hasattr(agent, "stats") else {},
            }
            for chat_id, agent in self._agents.items()
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
