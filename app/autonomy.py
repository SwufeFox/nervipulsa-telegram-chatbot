"""The autonomous participation loop: observe, wake, decide, act, wait.

What this module replaces
-------------------------
The old path asked `ParticipationPolicy` whether the bot was *allowed* to
think: a keyword hit, a weighted score, or a cost brake decided that a model
call should happen, and only then did the model write a reply. The model could
not decline, and it could not speak unless the rules had already said yes.

Here the order is inverted. Every authorized observation enters a durable
stream. A scheduler decides *when* the agent gets an opportunity - batching,
fairness, budgets - and the agent then decides what to do with that
opportunity: send a message, deliberately finish without one, wait for a
future event, or record a note for later. Nothing in this file scores how
interesting a message is.

Three things are kept strictly apart, because collapsing them is how
"autonomy" turns back into an auto-reply bot:

  received      the update is durably in `observation_events`
  presented     those rows were part of a model request (workflow range)
  acted         the agent chose a tool, and the effect was executed

A gap in any of them is recorded with a reason. Silence is never inferred
from a missing turn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

log = logging.getLogger(__name__)

# Batch shape. These bound cost and latency; they do not judge content.
DEFAULT_IDLE_SECONDS = 2.0
DEFAULT_MAX_BATCH_SECONDS = 15.0
DEFAULT_MAX_BATCH_OBSERVATIONS = 24
DEFAULT_REFRESH_LIMIT = 2


@dataclass
class Observation:
    sequence: int
    chat_id: int
    topic_id: int | None
    kind: str
    payload: dict[str, Any]
    received_at: float


@dataclass
class WorkflowResult:
    """What one autonomous turn concluded.

    `state` is the scheduler's view (completed/deferred/failed); `outcome` is
    the agent's decision. They are separate on purpose: a turn can complete
    normally and still have decided to say nothing.
    """

    workflow_id: str
    state: str
    outcome: str | None = None
    actions: list[str] = field(default_factory=list)
    waits: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Batch:
    chat_id: int
    topic_id: int | None
    rows: list[sqlite3_Row]  # type: ignore[name-defined]  # sqlite3.Row
    first_at: float


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _row_to_observation(row: Any) -> Observation:
    payload = json.loads(row["payload_json"])
    return Observation(
        sequence=int(row["sequence"]),
        chat_id=int(row["chat_id"]),
        topic_id=row["topic_id"],
        kind=str(row["kind"]),
        payload=payload if isinstance(payload, dict) else {"value": payload},
        received_at=float(row["received_at"]),
    )


def observation_kind_for(message: Any) -> str:
    """Structural label only. No content inspection, no scoring."""
    if getattr(message, "is_bot", False):
        return "bot_message"
    return "message"


class AutonomousLoop:
    """Owns batching, the agent call, tool effects and durable follow-ups."""

    def __init__(
        self,
        *,
        store: Any,
        agent_for: Callable[[int, int | None], Awaitable[Any | None]],
        system_prompt: str,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] | None = None,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        max_batch_seconds: float = DEFAULT_MAX_BATCH_SECONDS,
        max_batch_observations: int = DEFAULT_MAX_BATCH_OBSERVATIONS,
        refresh_limit: int = DEFAULT_REFRESH_LIMIT,
        send: Callable[..., Awaitable[Any]] | None = None,
        bot_name: str = "Alma",
        bot_user_id: int | None = None,
        max_activations_per_hour: int | None = 60,
        max_actions_per_hour: int | None = 20,
        min_action_interval_seconds: float = 20.0,
        max_concurrent_scopes: int = 4,
        max_actions_per_flush: int = 20,
    ) -> None:
        self.store = store
        self._agent_for = agent_for
        self.system_prompt = system_prompt
        self._clock = clock
        # Preserve callers that inject one fake clock for both time domains.
        self._monotonic = monotonic_clock or (time.monotonic if clock is time.time else clock)
        self.idle_seconds = idle_seconds
        self.max_batch_seconds = max_batch_seconds
        self.max_batch_observations = max_batch_observations
        self.refresh_limit = refresh_limit
        self._send = send
        self.bot_name = bot_name
        self.bot_user_id = bot_user_id
        self.max_activations_per_hour = max_activations_per_hour
        self.max_actions_per_hour = max_actions_per_hour
        self.min_action_interval_seconds = min_action_interval_seconds
        # How many scopes may be inside a model call at the same time. Topics
        # beyond this stay queued in `_batch_started` rather than becoming
        # tasks that only wait for a slot.
        self.max_concurrent_scopes = max(1, int(max_concurrent_scopes))
        # How many pending intents one outbox pass may examine. Bounded so a
        # single chat's throttled backlog cannot crowd out other chats.
        self.max_actions_per_flush = max(1, int(max_actions_per_flush))

        self._wake = asyncio.Event()
        # Opened/last-activity per scope, both on the injected clock. Using
        # SQLite's received_at here would mix two time bases: the store writes
        # wall-clock `time.time()`, while `_clock` is injectable (and a fake
        # clock in tests), so a batch could look perpetually fresh.
        self._batch_started: dict[tuple[int, int | None], float] = {}
        self._last_activity: dict[tuple[int, int | None], float] = {}
        # Explicit "do not look before" time, set when a budget defers a
        # scope. Kept separate from the idle window so a retry is not
        # immediately eligible just because the batch had already gone quiet.
        self._retry_after: dict[tuple[int, int | None], float] = {}
        self._locks: dict[tuple[int, int | None], asyncio.Lock] = {}
        self._flush_lock = asyncio.Lock()
        self._throttled_actions: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._active_topics: dict[tuple[int, int | None], asyncio.Task] = {}
        self._stopped = False

        # Counters the tests and diagnostics assert on.
        self.workflows = 0
        self.model_calls = 0
        self.actions_staged = 0
        self.actions_sent = 0
        self.silent_workflows = 0
        self.waits_created = 0
        self.deferred = 0

    # -- scheduling --------------------------------------------------------

    def note_activity(self, chat_id: int, topic_id: int | None) -> None:
        """An observation landed; start (or continue) a batch window."""
        key = (chat_id, topic_id)
        now = self._monotonic()
        self._batch_started.setdefault(key, now)
        self._last_activity[key] = now
        self._wake.set()

    def _batches(self, now: float) -> list[tuple[int, int | None]]:
        """Scopes whose window is ready: idle long enough, or open too long."""
        ready: list[tuple[int, int | None]] = []
        for key, opened in list(self._batch_started.items()):
            rows = self.store.pending_observations(
                key[0], key[1], limit=self.max_batch_observations
            )
            if not rows:
                self._batch_started.pop(key, None)
                self._last_activity.pop(key, None)
                continue
            if now < self._retry_after.get(key, 0.0):
                continue
            last_at = self._last_activity.get(key, opened)
            idle = now - last_at
            age = now - opened
            if idle >= self.idle_seconds or age >= self.max_batch_seconds:
                ready.append(key)
        return ready

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Wake on new observations or due timers; never busy-poll a model."""
        while not self._stopped:
            if stop is not None and stop.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            await self.tick()

    async def tick(self, *, force: bool = False,
                   await_tasks: bool = True) -> list[WorkflowResult]:
        """Process whatever is due. One pass; safe to call from tests.

        `force` closes every open window immediately (shutdown, and tests that
        drive a fake clock). It bypasses the debounce, never the ordering lock.

        `await_tasks=False` is the live mode: dispatch what fits the concurrency
        budget and return. A long model call must not hold the scheduler, or a
        scope that is queued behind the cap would never be picked up - the pass
        that would pick it up is the one still waiting for the model. The
        dispatched tasks re-arm the wake event on completion.
        """
        now = self._monotonic()
        if force:
            ready = list(self._batch_started)
        else:
            ready = self._batches(now)
        results: list[WorkflowResult | None] = []
        tasks: list[asyncio.Task] = []
        for key in ready:
            # The same topic is already being decided; a second task would only
            # queue behind the lock and grow the wait list. Scopes that do not
            # fit the concurrency budget are left in `_batch_started`, so the
            # next pass picks them up instead of spending a task to wait.
            if key in self._active_topics or len(self._active_topics) >= self.max_concurrent_scopes:
                continue
            task = asyncio.create_task(self.activate(*key))
            self._active_topics[key] = task

            def _finished(finished: asyncio.Task, scope: tuple[int, int | None] = key) -> None:
                self._active_topics.pop(scope, None)
                self._wake.set()

            task.add_done_callback(_finished)
            tasks.append(task)
        for wait in self.store.due_waits(self._clock()):
            self._wake_due(wait)
        if await_tasks and tasks:
            # Gather, not sequential awaits: two topics must be able to wait on
            # the model at the same time. Per-topic ordering still comes from
            # `activate()`'s lock, and the cap above bounds how many run at once.
            results = list(await asyncio.gather(*tasks))
        await self.flush_actions()
        return [result for result in results if result is not None]

    async def drain(self) -> None:
        """Let in-flight activations finish before the store is closed."""
        tasks = list(self._active_topics.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _wake_due(self, wait: Any) -> None:
        """A timer is a reason to look again, not permission to speak."""
        topic_id = wait["topic_id"]
        self.note_activity(int(wait["chat_id"]), topic_id)

    # -- budgets -----------------------------------------------------------

    def _budget_retry_delay(self) -> float:
        """How long to wait before retrying an activation budget refusal."""
        return 30.0

    def _failure_retry_delay(self) -> float:
        """How long to wait before retrying a turn that failed before deciding.

        Shorter than the budget retry: a provider hiccup or a missing runtime
        should not cost the agent half a minute of the conversation.
        """
        return 5.0

    def _paused_retry_delay(self) -> float:
        """A paused actor will refuse the same request; do not hammer it."""
        return 300.0

    def _budget_verdict(self, chat_id: int) -> str | None:
        """Return the model cost limit, independently of delivery limits."""
        now = self._clock()
        hour_ago = now - 3600
        if self.max_activations_per_hour is not None:
            used = self.store.count_activations_since(chat_id, hour_ago)
            if used >= self.max_activations_per_hour:
                return f"activations_per_hour({used}/{self.max_activations_per_hour})"
        return None

    def _pending_counts(self, chat_id: int, topic_id: int | None) -> tuple[int, int]:
        actions = self.store._query(
            "SELECT COUNT(*) AS n FROM actions WHERE chat_id = ? AND topic_id IS ? AND status = 'pending'",
            (chat_id, topic_id),
        )
        return int(actions[0]["n"]), len(self.store.active_waits(chat_id, topic_id))

    # -- one autonomous turn ----------------------------------------------

    def _requeue_after_failure(self, chat_id: int, topic_id: int | None,
                               *, activation_status: str | None = None) -> None:
        """Put a scope back in the queue after a turn failed without consuming input.

        A failed workflow does not advance the cursor, so its observations are
        still pending - but the batch window was already cleared before the
        call. Without re-arming, those observations would sit there until some
        unrelated message happened to arrive in the same topic, or the process
        restarted. Retry on a real interval so a broken provider cannot spin
        the scheduler.

        A *paused* actor is the exception. `response_unknown` and
        `commit_interrupted` mean Core deliberately stopped making requests:
        retrying them automatically would re-send a request whose answer may
        already exist, and Core itself refuses to resume on new input for those
        kinds. The scope is left queued (so nothing is lost and a restart or an
        operator retry can pick it up) but the retry clock is set far enough out
        that this loop does not spin against a decided "no".
        """
        now = self._monotonic()
        self._batch_started[(chat_id, topic_id)] = now
        paused = activation_status == "paused"
        self._retry_after[(chat_id, topic_id)] = now + (
            self._paused_retry_delay() if paused else self._failure_retry_delay()
        )
        if paused:
            log.warning("scope paused chat_id=%s topic_id=%s; not retrying until "
                        "the actor is explicitly resumed", chat_id, topic_id)
        self._wake.set()

    async def activate(self, chat_id: int, topic_id: int | None) -> WorkflowResult | None:
        key = (chat_id, topic_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return None
        async with lock:
            return await self._activate_locked(chat_id, topic_id)

    async def _activate_locked(self, chat_id: int, topic_id: int | None) -> WorkflowResult | None:
        rows = self.store.pending_observations(
            chat_id, topic_id, limit=self.max_batch_observations
        )
        if not rows:
            self._batch_started.pop((chat_id, topic_id), None)
            self._last_activity.pop((chat_id, topic_id), None)
            self._retry_after.pop((chat_id, topic_id), None)
            return None

        # A cost brake, not a social judgement. Observations stay queued and
        # keep their order; the only thing that is deferred is the model call.
        # Crucially this is recorded as `deferred_budget`, never as the agent
        # having chosen silence, and the scope is re-armed so it runs later.
        budget = self._budget_verdict(chat_id)
        if budget is not None:
            self.deferred += 1
            # Hold the scope for a real interval rather than retrying on every
            # 250 ms tick, which would spin the scheduler and flood the log
            # while the window is still closed.
            self._retry_after[(chat_id, topic_id)] = (
                self._monotonic() + self._budget_retry_delay()
            )
            self._batch_started[(chat_id, topic_id)] = self._monotonic()
            log.info("activation deferred chat_id=%s topic_id=%s reason=activation_budget observations=%s budget=%s first_sequence=%s last_sequence=%s retry_seconds=%s", chat_id, topic_id, len(rows), budget, rows[0]["sequence"], rows[-1]["sequence"], self._budget_retry_delay())
            return WorkflowResult(
                workflow_id="", state="deferred", outcome=budget,
                detail={"reason": budget, "pending": len(rows)},
            )

        observations = [_row_to_observation(row) for row in rows]
        workflow_id = self.store.open_workflow(
            chat_id=chat_id,
            topic_id=topic_id,
            first_sequence=observations[0].sequence,
            last_sequence=observations[-1].sequence,
            workflow_id=_new_id("wf"),
        )
        self._batch_started.pop((chat_id, topic_id), None)
        self._last_activity.pop((chat_id, topic_id), None)
        self._retry_after.pop((chat_id, topic_id), None)
        self.workflows += 1
        log.info("batch activated chat_id=%s topic_id=%s workflow_id=%s observations=%s first_sequence=%s last_sequence=%s", chat_id, topic_id, workflow_id, len(observations), observations[0].sequence, observations[-1].sequence)

        # Mark any wait that this batch structurally satisfies. The agent is
        # woken by the events themselves; the wait is not a decision.
        for observation in observations:
            self.store.satisfy_event_waits(
                chat_id, topic_id, observation.kind, self._clock()
            )

        agent = await self._agent_for(chat_id, topic_id)
        if agent is None:
            log.info("activation ended chat_id=%s topic_id=%s workflow_id=%s outcome=no_agent", chat_id, topic_id, workflow_id)
            self.store.close_workflow(workflow_id, "failed", outcome="no_agent")
            self._requeue_after_failure(chat_id, topic_id)
            return WorkflowResult(workflow_id, "failed", "no_agent")

        handler = _ActionHandler(self, chat_id=chat_id, topic_id=topic_id,
                                workflow_id=workflow_id,
                                observed_through=observations[-1].sequence)
        observation_payload = self._render(observations, chat_id, topic_id)

        started = time.monotonic()
        log.info("agent call started chat_id=%s topic_id=%s workflow_id=%s", chat_id, topic_id, workflow_id)
        try:
            if hasattr(agent, "observe"):
                result = await agent.observe(observation_payload, tag=workflow_id,
                                             handler=handler)
            else:  # scripted stand-in in tests
                result = await agent.ask(chat_id=chat_id,
                                         prompt=json.dumps(observation_payload, ensure_ascii=False),
                                         tag=workflow_id)
        except Exception as exc:
            log.info("agent call ended chat_id=%s topic_id=%s workflow_id=%s outcome=agent_error error_type=%s elapsed_ms=%.1f pending_actions=%s waits=%s", chat_id, topic_id, workflow_id, type(exc).__name__, (time.monotonic() - started) * 1000, *self._pending_counts(chat_id, topic_id))
            self.store.close_workflow(workflow_id, "failed",
                                      outcome="agent_error", detail={"error": str(exc)[:500]})
            self._requeue_after_failure(chat_id, topic_id)
            return WorkflowResult(workflow_id, "failed", "agent_error")

        self.model_calls += 1
        status = getattr(result, "status", "error")
        if status in {"error", "timeout", "rejected", "paused"}:
            log.info("agent call ended chat_id=%s topic_id=%s workflow_id=%s outcome=model_activation_failed activation_status=%s elapsed_ms=%.1f pending_actions=%s waits=%s", chat_id, topic_id, workflow_id, status, (time.monotonic() - started) * 1000, *self._pending_counts(chat_id, topic_id))
            # A provider failure is not the agent choosing silence.
            self.store.close_workflow(
                workflow_id, "failed", outcome="model_activation_failed",
                detail={"activation_status": status,
                        "detail": getattr(result, "detail", {})},
            )
            # `status="error"` carries the pause in `detail.error_kind`; only
            # `status="paused"` is the actor-refused case the agent reports
            # separately. Both mean Core may be holding a request whose result
            # is unknown, so neither should be retried on the fast path.
            detail = getattr(result, "detail", {}) or {}
            error_kind = detail.get("error_kind") if isinstance(detail, dict) else None
            paused = status == "paused" or error_kind in {
                "response_unknown", "commit_interrupted", "response_commit_failed",
            }
            self._requeue_after_failure(
                chat_id, topic_id, activation_status="paused" if paused else status
            )
            return WorkflowResult(workflow_id, "failed", "model_activation_failed")

        outcome = getattr(result, "outcome", None) or handler.outcome or "no_action"
        logged_outcome = outcome if isinstance(outcome, str) and outcome in {"silent", "no_action", "speak", "waiting"} else "other"
        log.info("agent call ended chat_id=%s topic_id=%s workflow_id=%s outcome=%s elapsed_ms=%.1f pending_actions=%s waits=%s", chat_id, topic_id, workflow_id, logged_outcome, (time.monotonic() - started) * 1000, *self._pending_counts(chat_id, topic_id))
        if handler.actions:
            self.actions_staged += len(handler.actions)
        if outcome in {"silent", "no_action"} and not handler.actions:
            self.silent_workflows += 1
        self.waits_created += len(handler.waits)

        detail = {"activation": getattr(result, "detail", {}),
                  "observations": [o.sequence for o in observations]}
        self.store.close_workflow(workflow_id, "completed", outcome=outcome, detail=detail)

        # Only now, with every intent already durable AND the cursor advanced,
        # may effects run. Executing before the close would compare the action
        # against a watermark that has not yet moved, so an action decided on
        # its own batch would look stale to itself. Observations that arrived
        # during the turn stay past the new cursor and are exactly what makes
        # a strict action stale - the case this ordering is meant to catch.
        await self.flush_actions()

        # If more observations arrived while we were thinking, keep going:
        # same topic stays ordered through the lock, other topics are free.
        if self.store.pending_observations(chat_id, topic_id, limit=1):
            self._batch_started[(chat_id, topic_id)] = self._monotonic()

        return WorkflowResult(
            workflow_id=workflow_id, state="completed", outcome=outcome,
            actions=list(handler.actions), waits=list(handler.waits),
            states=list(handler.states), detail=detail,
        )

    def _render(self, observations: Sequence[Observation], chat_id: int,
                topic_id: int | None) -> dict[str, Any]:
        """Structured, not a fake user prompt.

        The agent gets event ids and provenance so it can reason about what it
        actually saw. Deliberately no "--- new messages ---" wrapper: these are
        observations, not a request to reply.
        """
        state = self.store.get_state(chat_id, topic_id)
        waits = [
            {"wait_id": str(row["wait_id"]), "kind": str(row["kind"]),
             "purpose": str(row["purpose"])}
            for row in self.store.active_waits(chat_id, topic_id)
        ]
        return {
            "kind": "group_observations",
            "chat_id": chat_id,
            "topic_id": topic_id,
            "observations": [
                {"sequence": o.sequence, "type": o.kind, "received_at": o.received_at,
                 "payload": o.payload}
                for o in observations
            ],
            "state": state,
            "active_waits": waits,
            "note": (
                "These are group observations, not an instruction to reply. "
                "Decide whether to act; finish_silently is a valid outcome."
            ),
        }

    # -- outbox ------------------------------------------------------------

    async def flush_actions(self) -> None:
        """Execute only durably recorded intents, one atomic claim at a time.

        Serialized across callers: two topics may finish their turns at the
        same moment, and both would otherwise try to claim the same pending
        row. The SQLite claim is already atomic, but the send itself must not
        be attempted twice from two coroutines in this process.
        """
        async with self._flush_lock:
            await self._flush_actions_locked()

    def _pending_actions_fair(self) -> list[Any]:
        """Pending intents, interleaved across chats, oldest first within a chat.

        Ordering purely by `created_at` lets one chat's throttled backlog occupy
        every slot of a bounded pass, so another chat's single sendable message
        is never even examined. Taking the oldest intent per chat first, then
        the second, and so on, gives each chat a turn.
        """
        return self.store._query(
            "SELECT * FROM ("
            "  SELECT a.*, ROW_NUMBER() OVER ("
            "    PARTITION BY a.chat_id ORDER BY a.created_at, a.action_id"
            "  ) AS turn FROM actions a WHERE a.status = 'pending'"
            ") WHERE turn <= ? ORDER BY turn, created_at LIMIT ?",
            (self.max_actions_per_flush, self.max_actions_per_flush),
        )

    async def _flush_actions_locked(self) -> None:
        # Every budget and freshness decision lives inside `claim_action`'s
        # write transaction. The list only bounds *work per pass*: without a
        # cap, a chat with a large throttled backlog would fill every pass and
        # the one sendable action from another chat would never be reached.
        pending = self._pending_actions_fair()
        for row in pending:
            action_id = str(row["action_id"])
            # The store re-checks freshness AND the delivery budget inside the
            # same write transaction that flips pending -> sending. Checking
            # here first would only be an optimisation; two processes sharing
            # the database would still both pass a check made outside it.
            verdict = self.store.claim_action(
                action_id,
                max_actions_per_hour=self.max_actions_per_hour,
                min_action_interval_seconds=self.min_action_interval_seconds,
            )
            if verdict == "throttled":
                if action_id not in self._throttled_actions:
                    log.info("action throttled chat_id=%s topic_id=%s action_id=%s",
                             row["chat_id"], row["topic_id"], action_id)
                    self._throttled_actions.add(action_id)
                continue
            self._throttled_actions.discard(action_id)
            logged_verdict = verdict if verdict in {"claimed", "stale", "expired"} else "other"
            log.info("action claim chat_id=%s topic_id=%s action_id=%s outcome=%s", row["chat_id"], row["topic_id"], action_id, logged_verdict)
            if verdict == "stale":
                # Same-topic information arrived that this intent never saw.
                # Give the agent another look instead of sending stale text.
                self.store.finish_action(action_id, "cancelled",
                                         error="needs_refresh: unobserved events")
                self.note_activity(int(row["chat_id"]), row["topic_id"])
                continue
            if verdict == "expired":
                continue
            if verdict != "claimed":
                continue
            if self._send is None:
                self.store.finish_action(action_id, "failed", error="no transport")
                self._report_result(row, action_id, "failed", "no transport")
                continue
            try:
                outcome = await self._send(
                    chat_id=int(row["chat_id"]),
                    topic_id=row["topic_id"],
                    text=str(row["text"]),
                    reply_to=row["reply_to"],
                )
            except Exception as exc:
                # Unknown, not failed: the request may have reached Telegram.
                log.info("action delivery chat_id=%s topic_id=%s action_id=%s outcome=unknown error_type=%s", row["chat_id"], row["topic_id"], action_id, type(exc).__name__)
                detail = str(exc)[:500]
                self.store.finish_action(action_id, "unknown", error=detail)
                self._report_result(row, action_id, "unknown", detail)
                continue
            status = getattr(outcome, "status", None)
            name = getattr(status, "value", status)
            error = getattr(outcome, "error", None)
            log.info("action delivery chat_id=%s topic_id=%s action_id=%s outcome=%s", row["chat_id"], row["topic_id"], action_id, name if name in ("sent", "unknown") else "failed")
            if name == "sent":
                self.actions_sent += 1
                telegram_id = getattr(outcome, "telegram_message_id", None)
                self.store.finish_action(action_id, "sent",
                                         telegram_message_id=telegram_id,
                                         sender_label=self.bot_name, sender_id=self.bot_user_id)
                self._report_result(row, action_id, "sent", None)
            elif name == "unknown":
                self.store.finish_action(action_id, "unknown", error=error)
                self._report_result(row, action_id, "unknown", error)
            elif name == "failed":
                self.store.finish_action(action_id, "failed", error=error)
                self._report_result(row, action_id, "failed", error)
            else:
                self.store.finish_action(action_id, "unknown", error=error or "unrecognized transport outcome")
                self._report_result(row, action_id, "unknown", error or "unrecognized transport outcome")

    def _report_result(self, row: Any, action_id: str, status: str,
                       error: str | None) -> None:
        """Feed a *surprising* delivery outcome back as an observation.

        Only failures and unconfirmed deliveries become observations. A
        successful send tells the agent nothing she does not already know -
        she chose to speak and the tool said it was accepted - and putting it
        in the stream would wake her on her own message, which is how a bot
        starts answering itself. The sent message still enters the chat log,
        so later turns see it as history.

        The two cases that do get reported are exactly the ones that change
        what she believes about the conversation: a message that failed to
        send, and one whose delivery is unknown, where the group may be
        looking at a sentence she thinks never arrived.
        """
        if status == "sent":
            return
        chat_id = int(row["chat_id"])
        topic_id = row["topic_id"]
        log.warning("action result action_id=%s chat_id=%s status=%s", action_id, chat_id, status)
        self.note_activity(chat_id, topic_id)


class _ActionHandler:
    """Bridges model tool calls to durable intent. No Telegram here."""

    def __init__(self, loop: AutonomousLoop, *, chat_id: int, topic_id: int | None,
                 workflow_id: str, observed_through: int) -> None:
        self._loop = loop
        self.chat_id = chat_id
        self.topic_id = topic_id
        self.workflow_id = workflow_id
        self.observed_through = observed_through
        self.actions: list[str] = []
        self.waits: list[str] = []
        self.states: list[str] = []
        self.outcome: str | None = None

    async def __call__(self, name: str, arguments: dict[str, Any], context: Any,
                       activation: Any, call: Any) -> tuple[dict[str, Any], str]:
        if name == "send_message":
            return self._send_message(arguments)
        if name == "finish_silently":
            self.outcome = "silent"
            return ({"status": "accepted", "outcome": "silent",
                     "executed": False}, "accepted")
        if name == "wait_for_event":
            return self._wait_event(arguments)
        if name == "wait_until":
            return self._wait_until(arguments)
        if name == "update_state":
            return self._update_state(arguments)
        return ({"status": "rejected", "reason": "unknown_tool",
                 "executed": False}, "rejected")

    def _send_message(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        action_id = _new_id("act")
        freshness = str(arguments.get("freshness") or "strict")
        cap = int(arguments.get("max_unobserved_events") or 0)
        expires_at = None
        if freshness == "bounded":
            # A bounded action still cannot outlive a sane wall-clock window.
            expires_at = self._loop._clock() + 3600
        created = self._loop.store.prepare_action(
            action_id=action_id,
            workflow_id=self.workflow_id,
            chat_id=self.chat_id,
            topic_id=self.topic_id,
            text=str(arguments["text"]),
            observed_through=self.observed_through,
            reply_to=arguments.get("reply_to"),
            freshness=freshness,
            max_unobserved=cap,
            expires_at=expires_at,
        )
        self.actions.append(action_id)
        self.outcome = self.outcome or "speak"
        # "accepted" means the intent is durable. Delivery is not implied.
        return ({"status": "accepted", "action_id": action_id,
                 "executed": False, "delivery": "pending",
                 "detail": ("recorded" if created else "already recorded")}, "accepted")

    def _wait_event(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        wait_id = _new_id("wait")
        expires_at = _seconds_from_iso(arguments.get("expires_at"))
        self._loop.store.add_wait(
            wait_id=wait_id, chat_id=self.chat_id, topic_id=self.topic_id,
            kind="event", purpose=str(arguments["purpose"]),
            event_types=arguments.get("event_types"), expires_at=expires_at,
        )
        self.waits.append(wait_id)
        self.outcome = self.outcome or "waiting"
        return ({"status": "accepted", "wait_id": wait_id,
                 "executed": False}, "accepted")

    def _wait_until(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        wait_id = _new_id("wait")
        due_at = _seconds_from_iso(arguments["at"])
        expires_at = _seconds_from_iso(arguments.get("expires_at"))
        self._loop.store.add_wait(
            wait_id=wait_id, chat_id=self.chat_id, topic_id=self.topic_id,
            kind="until", purpose=str(arguments.get("purpose") or "reconsider"),
            due_at=due_at, expires_at=expires_at,
        )
        self.waits.append(wait_id)
        self.outcome = self.outcome or "waiting"
        return ({"status": "accepted", "wait_id": wait_id,
                 "executed": False}, "accepted")

    def _update_state(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        expires_at = _seconds_from_iso(arguments.get("expires_at"))
        self._loop.store.set_state(
            chat_id=self.chat_id, topic_id=self.topic_id,
            key=str(arguments["key"]), value=arguments["value"],
            source="agent", expires_at=expires_at,
        )
        self.states.append(str(arguments["key"]))
        return ({"status": "accepted", "key": str(arguments["key"]),
                 "executed": False}, "accepted")


def _seconds_from_iso(value: Any) -> float | None:
    from .autonomous_tools import _aware_timestamp
    return _aware_timestamp(value)
