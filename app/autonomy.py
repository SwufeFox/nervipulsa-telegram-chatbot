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
    # The durable logical work this activation belonged to. Two results with
    # the same episode are the same job continuing, not two separate turns.
    episode_id: str | None = None


@dataclass
class _Batch:
    chat_id: int
    topic_id: int | None
    rows: list[sqlite3_Row]  # type: ignore[name-defined]  # sqlite3.Row
    first_at: float


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _label(kinds: dict[int, str], chat_id: int, topic_id: int | None) -> str:
    from .logwatch import scope

    return scope(chat_id, topic_id, kinds.get(chat_id))


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
        # chat_id -> Bot API chat type, supplied by the ingest path so log
        # lines can say `chat:-100…(group)` instead of a bare id.
        self.chat_kinds: dict[int, str] = {}
        # Characters of message text to preview in logs. 0 means never.
        self.summary_chars = 0
        # A reply older than this is no longer a reply to anything: in a live
        # group, minutes-old quips are worse than silence. This - not a message
        # count - is what makes a default reply stale.
        self.default_reply_ttl = 180.0
        # How long an explicitly `bounded` intent may wait before it is dropped
        # unset.
        # Deliberately longer than the hourly send budget: the budget is the
        # thing that should decide whether a held reply still goes out.
        self.bounded_action_ttl = 2 * 3600.0

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
        # Timers first: `_wake_due` writes the wake as an observation, so a
        # later pass would have to exist before the agent could act on its own
        # reminder.
        # Goals lapse on their own schedule and must be reported, not silently
        # dropped: a context still holding an expired plan is worse than no plan.
        for expired in self.store.expire_intents(self._clock()):
            self._report_intent_expiry(expired)
        fired: list[tuple[int, int | None]] = []
        for wait in self.store.due_waits(self._clock()):
            key = self._wake_due(wait)
            if key is not None:
                fired.append(key)
        if force:
            ready = list(self._batch_started)
        else:
            ready = self._batches(now)
        # A fired timer is a scheduled wake, not a burst of chat: there is
        # nothing to wait for. `_wake_due` necessarily opens a fresh batch
        # window (idle == 0), which the debounce would reject, costing an extra
        # pass. Dispatch these scopes now.
        for key in fired:
            if not force and now < self._retry_after.get(key, 0.0):
                continue
            if key not in ready and self.store.pending_observations(
                    key[0], key[1], limit=1):
                ready.append(key)
        results: list[WorkflowResult | None] = []
        tasks: list[asyncio.Task] = []
        for key in ready:
            # The same topic is already being decided; a second task would only
            # queue behind the lock and grow the wait list. Scopes that do not
            # fit the concurrency budget are left in `_batch_started`, so the
            # next pass picks them up instead of spending a task to wait.
            # A finished task's cleanup runs as a *callback*, which the event
            # loop schedules for later - not by the time `gather` returns. So a
            # just-finished scope still sits in the dict, and trusting the dict
            # alone made the next pass skip exactly the work that had become
            # ready. Check the task, and evict a dead entry instead of dropping
            # the scope.
            existing = self._active_topics.get(key)
            if existing is not None:
                if not existing.done():
                    continue
                self._active_topics.pop(key, None)
            if len(self._active_topics) >= self.max_concurrent_scopes:
                # No slot free: the scope stays in `_batch_started`, so the next
                # pass takes it rather than spending a task to queue behind one.
                continue
            task = asyncio.create_task(self.activate(*key))
            self._active_topics[key] = task

            def _finished(finished: asyncio.Task, scope: tuple[int, int | None] = key) -> None:
                if self._active_topics.get(scope) is finished:
                    self._active_topics.pop(scope, None)
                self._wake.set()

            task.add_done_callback(_finished)
            tasks.append(task)
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

    def _wake_due(self, wait: Any) -> tuple[int, int | None] | None:
        """A timer the agent registered has come due: deliver it as input.

        Before this, a due timer only re-armed the batch window, and `_batches`
        threw that window away for having no pending observations. So
        `wait_until` was decoration: the agent's own "look again in two minutes"
        fired only if somebody unrelated happened to speak, and the wait row was
        never resolved, so `due_waits` returned it on every tick forever - the
        scheduler woke, found nothing to do, and woke again.

        Two fixes, in this order on purpose:

        1. Write the wake as a durable observation *first*. If the process dies
           before the wait is resolved, the next tick re-attempts it and the
           dedupe key keeps the wake from appearing twice.
        2. Resolve the wait, so it fires exactly once.

        The payload carries the agent's own stated purpose, because an
        activation with no new messages and no explanation is indistinguishable
        from being interrupted at random.
        """
        chat_id = int(wait["chat_id"])
        topic_id = wait["topic_id"]
        wait_id = str(wait["wait_id"])
        now = self._clock()
        due_at = wait["due_at"]
        expires_at = wait["expires_at"]

        # A due timer and a closed window are different events: one means "the
        # moment you asked for arrived", the other "the chance passed".
        due = due_at is not None and float(due_at) <= now
        status = "satisfied" if due else "expired"
        if not due and (expires_at is None or float(expires_at) > now):
            return None  # nothing has actually happened yet

        self.store.append_synthetic_observation(
            dedupe_key=f"wait:{wait_id}",
            chat_id=chat_id,
            topic_id=topic_id,
            kind="timer",
            payload={
                "kind": "timer",
                "status": status,
                "wait_id": wait_id,
                "wait_kind": str(wait["kind"]),
                "purpose": str(wait["purpose"]),
                "due_at": due_at,
                "note": (
                    "The timer you set earlier has come due. Nothing new may "
                    "have been said since; reconsider what you were waiting to "
                    "do about it."
                    if due else
                    "The window you set for this wait has closed without it "
                    "being satisfied."
                ),
            },
        )
        self.store.resolve_wait(wait_id, status)
        self.note_activity(chat_id, topic_id)
        log.info("timer fired %s wait_id=%s status=%s purpose=%r",
                 _label(self.chat_kinds, chat_id, topic_id), wait_id, status,
                 str(wait["purpose"])[:60])
        # Returned so `tick()` can dispatch this scope without a debounce round
        # trip; the timer *is* the moment it asked for.
        return chat_id, topic_id

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

    @staticmethod
    def _undecided(handler: Any) -> bool:
        """True when a turn produced no decision at all.

        Any tool that records something counts: an action, a wait, a state
        note, or an explicit `finish_silently`. Text alone does not - ordinary
        assistant text is never delivered, so leaving it at that means the turn
        evaporated.
        """
        return not (handler.actions or handler.waits or handler.states
                    or handler.outcome or getattr(handler, "intent_set", None)
                    or getattr(handler, "intent_resolved", None))

    def _decision_reminder(self, payload: dict[str, Any], result: Any) -> dict[str, Any]:
        """One explicit second chance to decide, carrying what went wrong.

        Bounded to a single retry: a model that keeps answering in prose is
        misbehaving, and retrying forever would spend the budget on it.
        """
        produced = (getattr(result, "text", None) or "").strip()
        reminder = dict(payload)
        reminder["kind"] = "decision_required"
        reminder["note"] = (
            "Your previous response"
            + (f" ({len(produced)} characters of text)" if produced else "")
            + " did not call any tool. Text is never delivered to the chat, and "
            "nothing was recorded, so that turn had no effect.\n"
            "Call exactly one tool now: send_message to speak, "
            "finish_silently to end without speaking, wait_for_event / "
            "wait_until to be woken later, or update_state to remember "
            "something."
        )
        reminder["observations"] = payload.get("observations", [])
        return reminder

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
            log.warning("activation deferred %s reason=activation_budget budget=%s "
                        "observations=%s seq=%s-%s retry_seconds=%s",
                        _label(self.chat_kinds, chat_id, topic_id), budget, len(rows),
                        rows[0]["sequence"], rows[-1]["sequence"],
                        self._budget_retry_delay())
            return WorkflowResult(
                workflow_id="", state="deferred", outcome=budget,
                detail={"reason": budget, "pending": len(rows)},
            )

        observations = [_row_to_observation(row) for row in rows]
        # The logical work this activation belongs to. Reading first is what
        # tells a continuation apart from a fresh start: an episode already
        # open here means this wake is the same job carrying on.
        existing_episode = self.store.current_episode(chat_id, topic_id)
        episode_id = str(existing_episode["episode_id"]) if existing_episode else None
        continuation = episode_id is not None
        episode = self.store.open_episode(
            episode_id=_new_id("ep"), chat_id=chat_id, topic_id=topic_id,
        )
        # `str(None)` is the literal "None", which would look like a real id and
        # merge unrelated work into it. Treat a missing id as "no episode".
        episode_id = str(episode["episode_id"]) if episode["episode_id"] else None
        workflow_id = self.store.open_workflow(
            chat_id=chat_id,
            topic_id=topic_id,
            first_sequence=observations[0].sequence,
            last_sequence=observations[-1].sequence,
            workflow_id=_new_id("wf"),
            episode_id=episode_id,
        )
        self._batch_started.pop((chat_id, topic_id), None)
        self._last_activity.pop((chat_id, topic_id), None)
        self._retry_after.pop((chat_id, topic_id), None)
        self.workflows += 1
        # How far behind this batch was, and how much is still queued after
        # it: the backlog number is what turns "it looks fine" into "it is 40
        # minutes behind".
        # Prefer the message's own timestamp: after a restart the whole backlog
        # is ingested at once, so `received_at` would claim a zero lag for
        # messages that are 40 minutes old.
        moments = [
            float(o.payload.get("created_at") or o.received_at)
            for o in observations
        ]
        oldest = min(moments, default=0.0)
        lag = max(0.0, time.time() - oldest) if oldest else 0.0
        # Count what is left *after* this batch, not what the cursor says: the
        # workflow is still running, so the cursor has not moved yet and would
        # report this very batch as backlog.
        remaining = len(self.store._query(
            "SELECT 1 FROM observation_events"
            " WHERE chat_id = ? AND topic_id IS ? AND sequence > ? LIMIT ?",
            (chat_id, topic_id, observations[-1].sequence,
             self.max_batch_observations),
        ))
        log.info("batch activated %s workflow=%s episode=%s continuation=%s "
                 "observations=%s seq=%s-%s oldest_lag=%.0fs backlog_after=%s",
                 _label(self.chat_kinds, chat_id, topic_id), workflow_id, episode_id,
                 continuation, len(observations), observations[0].sequence,
                 observations[-1].sequence, lag, remaining)

        # Mark any wait that this batch structurally satisfies. The agent is
        # woken by the events themselves; the wait is not a decision.
        for observation in observations:
            self.store.satisfy_event_waits(
                chat_id, topic_id, observation.kind, self._clock()
            )

        agent = await self._agent_for(chat_id, topic_id)
        if agent is None:
            log.info("activation ended %s workflow=%s outcome=no_agent",
                 _label(self.chat_kinds, chat_id, topic_id), workflow_id)
            self.store.close_workflow(workflow_id, "failed", outcome="no_agent")
            self._requeue_after_failure(chat_id, topic_id)
            return WorkflowResult(workflow_id, "failed", "no_agent")

        handler = _ActionHandler(self, chat_id=chat_id, topic_id=topic_id,
                                workflow_id=workflow_id,
                                observed_through=observations[-1].sequence,
                                episode_id=episode_id)
        observation_payload = self._render(observations, chat_id, topic_id,
                                           episode_id=episode_id,
                                           continuation=continuation)

        started = time.monotonic()
        log.info("agent call started %s workflow=%s",
                 _label(self.chat_kinds, chat_id, topic_id), workflow_id)
        try:
            if hasattr(agent, "observe"):
                result = await agent.observe(observation_payload, tag=workflow_id,
                                             handler=handler)
            else:  # scripted stand-in in tests
                result = await agent.ask(chat_id=chat_id,
                                         prompt=json.dumps(observation_payload, ensure_ascii=False),
                                         tag=workflow_id)
        except Exception as exc:
            log.info("agent call ended %s workflow=%s outcome=agent_error error_type=%s "
                 "elapsed_ms=%.1f pending_actions=%s waits=%s",
                 _label(self.chat_kinds, chat_id, topic_id), workflow_id,
                 type(exc).__name__, (time.monotonic() - started) * 1000,
                 *self._pending_counts(chat_id, topic_id))
            self.store.close_workflow(workflow_id, "failed",
                                      outcome="agent_error", detail={"error": str(exc)[:500]})
            self._requeue_after_failure(chat_id, topic_id)
            return WorkflowResult(workflow_id, "failed", "agent_error")

        self.model_calls += 1
        status = getattr(result, "status", "error")
        # A successful activation that called no tool and left no other trace
        # did not reach a decision. It may have written text, and that text was
        # never delivered - so it is not silence, and recording it as
        # `no_action` invented a conclusion the model never reached. Ask once,
        # explicitly, before calling the turn anything.
        if status in {"ok", "committed", "succeeded"} and self._undecided(handler):
            log.info("activation produced no decision %s workflow=%s; asking once",
                     _label(self.chat_kinds, chat_id, topic_id), workflow_id)
            try:
                if hasattr(agent, "observe"):
                    result = await agent.observe(
                        self._decision_reminder(observation_payload, result),
                        tag=workflow_id, handler=handler,
                    )
                    self.model_calls += 1
                    status = getattr(result, "status", "error")
            except Exception as exc:
                log.warning("decision reminder failed %s workflow=%s error_type=%s",
                            _label(self.chat_kinds, chat_id, topic_id),
                            workflow_id, type(exc).__name__)

        if status in {"error", "timeout", "rejected", "paused"}:
            log.info("agent call ended %s workflow=%s outcome=model_activation_failed "
                 "activation_status=%s elapsed_ms=%.1f pending_actions=%s waits=%s",
                 _label(self.chat_kinds, chat_id, topic_id), workflow_id, status,
                 (time.monotonic() - started) * 1000,
                 *self._pending_counts(chat_id, topic_id))
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

        # The outcome is the agent's, never a default the application supplies.
        # `noted` covers a turn that only wrote state down; `undecided` is the
        # honest label for one that still chose nothing after being asked.
        outcome = handler.outcome or ("noted" if handler.states else "undecided")
        if outcome == "undecided":
            # Audit only - deliberately NOT a new observation. An observation
            # would immediately wake her again, and a model that answers in
            # prose twice would then write another notice, forever, at two
            # model calls per cycle. `observation_skips` is the ledger for
            # "recorded with a reason, never silent" that does not queue a turn.
            self.store.record_observation_skip(
                sequence=observations[-1].sequence, chat_id=chat_id,
                topic_id=topic_id,
                reason="undecided: no tool call after an explicit reminder",
            )
            log.warning("turn undecided %s workflow=%s; recorded as a skip, "
                        "not replayed", _label(self.chat_kinds, chat_id, topic_id),
                        workflow_id)
        known_outcomes = {"silent", "no_action", "speak", "waiting",
                          "noted", "undecided"}
        logged_outcome = outcome if isinstance(outcome, str) and outcome in known_outcomes else "other"
        log.info("agent call ended %s workflow=%s outcome=%s elapsed_ms=%.1f "
                 "pending_actions=%s waits=%s",
                 _label(self.chat_kinds, chat_id, topic_id), workflow_id, logged_outcome,
                 (time.monotonic() - started) * 1000,
                 *self._pending_counts(chat_id, topic_id))
        if handler.actions:
            self.actions_staged += len(handler.actions)
        if outcome in {"silent", "no_action"} and not handler.actions:
            self.silent_workflows += 1
        self.waits_created += len(handler.waits)

        detail = {"activation": getattr(result, "detail", {}),
                  "observations": [o.sequence for o in observations],
                  "episode_id": episode_id, "continuation": continuation}
        self.store.close_workflow(workflow_id, "completed", outcome=outcome, detail=detail)
        self._settle_episode(chat_id, topic_id, episode_id, outcome=outcome)

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
            states=list(handler.states), detail=detail, episode_id=episode_id,
        )

    def _render(self, observations: Sequence[Observation], chat_id: int,
                topic_id: int | None, *, episode_id: str | None = None,
                continuation: bool = False) -> dict[str, Any]:
        """Structured, not a fake user prompt.

        The agent gets event ids and provenance so it can reason about what it
        actually saw. Deliberately no "--- new messages ---" wrapper: these are
        observations, not a request to reply.
        """
        state = self.store.get_state(chat_id, topic_id)
        # The scope's unfinished goal, if there is one. This is what makes a
        # wake a continuation rather than a fresh decision task: without it the
        # agent sees new events plus some key/value notes and has to guess why
        # it is being called. Read on every turn, not only after a wait, since
        # an intent outlives any particular wait.
        intent = self.store.current_intent(chat_id, topic_id)
        intent_payload = None
        if intent is not None:
            intent_payload = {
                "intent_id": str(intent["intent_id"]),
                "goal": str(intent["goal"]),
                "opened_at": float(intent["created_at"]),
                "expires_at": intent["expires_at"],
            }
        # The logical work this scope is already in the middle of. This is the
        # difference between "you were woken" and "you were woken to continue
        # what you were doing": an activation opens a fresh decision task, an
        # episode carries the reason across them.
        episode_payload = None
        if episode_id is not None:
            row = self.store.current_episode(chat_id, topic_id)
            if row is not None:
                episode_payload = {
                    "episode_id": episode_id,
                    "opened_at": float(row["opened_at"]),
                    "activations": len(self.store.episode_activations(episode_id)),
                    "continuation": continuation,
                }
        waits = [
            {"wait_id": str(row["wait_id"]), "kind": str(row["kind"]),
             "purpose": str(row["purpose"])}
            for row in self.store.active_waits(chat_id, topic_id)
        ]
        return {
            "kind": "group_observations",
            "chat_id": chat_id,
            "topic_id": topic_id,
            "episode": episode_payload,
            "observations": [
                {"sequence": o.sequence, "type": o.kind, "received_at": o.received_at,
                 "payload": o.payload}
                for o in observations
            ],
            "state": state,
            "active_waits": waits,
            "open_intent": intent_payload,
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
                    log.info("action throttled %s action_id=%s",
                             _label(self.chat_kinds, int(row["chat_id"]), row["topic_id"]), action_id)
                    self._throttled_actions.add(action_id)
                continue
            self._throttled_actions.discard(action_id)
            logged_verdict = verdict if verdict in {"claimed", "stale", "expired"} else "other"
            log.info("action claim %s action_id=%s outcome=%s",
                     _label(self.chat_kinds, int(row["chat_id"]), row["topic_id"]),
                     action_id, logged_verdict)
            if verdict == "stale":
                # Same-topic information arrived that this intent never saw.
                # Give the agent another look instead of sending stale text.
                self.store.finish_action(action_id, "cancelled",
                                         error="needs_refresh: unobserved events")
                log.info("action dropped %s action_id=%s reason=conversation_moved_on",
                         _label(self.chat_kinds, int(row["chat_id"]), row["topic_id"]),
                         action_id)
                self._report_result(row, action_id, "cancelled",
                                    "the conversation moved on before this was sent")
                self.note_activity(int(row["chat_id"]), row["topic_id"])
                continue
            if verdict == "expired":
                # Same family as a stale drop: the reply was never sent, so
                # the agent must not go on believing she said it.
                log.info("action expired %s action_id=%s reason=too_late",
                         _label(self.chat_kinds, int(row["chat_id"]), row["topic_id"]),
                         action_id)
                self._report_result(row, action_id, "cancelled",
                                    "waited too long to send; dropped as stale")
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
                log.info("action delivery %s action_id=%s outcome=unknown error_type=%s",
                         _label(self.chat_kinds, int(row["chat_id"]), row["topic_id"]),
                         action_id, type(exc).__name__)
                detail = str(exc)[:500]
                self.store.finish_action(action_id, "unknown", error=detail)
                self._report_result(row, action_id, "unknown", detail)
                continue
            status = getattr(outcome, "status", None)
            name = getattr(status, "value", status)
            error = getattr(outcome, "error", None)
            log.info("action delivery %s action_id=%s outcome=%s",
                     _label(self.chat_kinds, int(row["chat_id"]), row["topic_id"]),
                     action_id, name if name in ("sent", "unknown") else "failed")
            if name == "sent":
                self.actions_sent += 1
                telegram_id = getattr(outcome, "telegram_message_id", None)
                self.store.finish_action(action_id, "sent",
                                         telegram_message_id=telegram_id,
                                         sender_label=self.bot_name, sender_id=self.bot_user_id)
                self._report_result(row, action_id, "sent", None)
                # The message landed, so a goal it claimed is now complete. Done
                # after delivery for the reason in `_close_intent_for_action`.
                self._close_intent_for_action(action_id)
            elif name == "unknown":
                self.store.finish_action(action_id, "unknown", error=error)
                self._report_result(row, action_id, "unknown", error)
            elif name == "failed":
                self.store.finish_action(action_id, "failed", error=error)
                self._report_result(row, action_id, "failed", error)
            else:
                self.store.finish_action(action_id, "unknown", error=error or "unrecognized transport outcome")
                self._report_result(row, action_id, "unknown", error or "unrecognized transport outcome")

    def _settle_episode(self, chat_id: int, topic_id: int | None,
                        episode_id: str | None, *, outcome: str) -> None:
        """Decide whether the logical work is finished, or still owed a wake.

        An episode that stays open is a promise that a later activation belongs
        to it, so leaving one open with nothing that could ever wake it is a
        leak, and closing one that is still waiting throws away the thread. The
        rule is therefore narrow: it stays open only while the agent has said
        something specific it still owes - an open goal, or a wait it registered.

        Pending observations deliberately do NOT count. New messages are new
        conversation, not this job carrying on; and in a busy group they are
        always present, so treating them as a reason would keep one episode open
        for days and make "continuation" meaningless again.
        """
        if not episode_id:
            return
        if self.store.current_intent(chat_id, topic_id) is not None:
            return
        if self.store.active_waits(chat_id, topic_id):
            return
        if self.store.close_episode(episode_id, reason=outcome):
            log.info("episode closed %s episode=%s reason=%s",
                     _label(self.chat_kinds, chat_id, topic_id), episode_id, outcome)

    def _close_intent_for_action(self, action_id: str) -> None:
        """Close the goal a message said it completes - but only once it landed.

        The model marks the message with `resolves_intent`; the goal closes
        here, not at tool time. A reply can still be dropped as stale, and
        closing on acceptance would mark a goal done for a sentence nobody saw,
        leaving nothing to revisit it.
        """
        try:
            rows = self.store._query(
                "SELECT intent_id FROM actions WHERE action_id = ?", (action_id,))
        except Exception as exc:
            # Storage trouble must not take down the sender, but staying silent
            # about it is how a goal ends up stranded.
            log.error("could not read the intent for action %s (error_type=%s)",
                      action_id, type(exc).__name__)
            return
        intent_id = str(rows[0]["intent_id"]) if rows and rows[0]["intent_id"] else None
        if not intent_id:
            return
        # `finish_action` closes the goal in the same transaction as the status
        # change, so this call is usually a no-op - and that is exactly why the
        # episode settlement cannot be nested inside the "did I close it" branch.
        # Once the goal is closed, nothing keeps the episode alive.
        if self.store.resolve_intent(intent_id, how="send_message"):
            log.info("intent closed action_id=%s intent_id=%s", action_id, intent_id)
        if self.store.intent_is_open(intent_id):
            return
        scope = self.store._query(
            "SELECT chat_id, topic_id FROM actions WHERE action_id = ?", (action_id,))
        if not scope:
            return
        episode = self.store.current_episode(
            int(scope[0]["chat_id"]), scope[0]["topic_id"])
        if episode is not None:
            self._settle_episode(
                int(scope[0]["chat_id"]), scope[0]["topic_id"],
                str(episode["episode_id"]), outcome="delivered",
            )

    def _report_intent_expiry(self, intent: Any) -> None:
        """Announce that a goal lapsed, as a durable observation.

        Expiry is not a failure, but it is not nothing: the goal is closed now,
        and a turn that still believed it open would keep reasoning towards
        something already given up. An observation survives a restart; a log
        line does not.
        """
        chat_id, topic_id = int(intent["chat_id"]), intent["topic_id"]
        try:
            self.store.append_synthetic_observation(
                dedupe_key=f"intent-expired:{intent['intent_id']}",
                chat_id=chat_id, topic_id=topic_id, kind="intent_expired",
                payload={
                    "intent_id": str(intent["intent_id"]),
                    "goal": str(intent["goal"]),
                    "note": (
                        "This goal reached its deadline and is closed. You do not "
                        "have to act on it; if it still matters, set it again."
                    ),
                },
            )
        except Exception as exc:
            log.error("could not record the expiry of intent %s (error_type=%s)",
                      intent["intent_id"], type(exc).__name__)
            return
        log.info("intent expired %s intent_id=%s",
                 _label(self.chat_kinds, chat_id, topic_id), intent["intent_id"])
        self.note_activity(chat_id, topic_id)

    def _report_result(self, row: Any, action_id: str, status: str,
                       error: str | None) -> None:
        """Feed a *surprising* delivery outcome back as an observation.

        Only failures and unconfirmed deliveries become observations. A
        successful send tells the agent nothing she does not already know -
        she chose to speak and the tool said it was accepted - and putting it
        in the stream would wake her on her own message, which is how a bot
        starts answering itself. The sent message still enters the chat log,
        so later turns see it as history.

        The cases that do get reported are exactly the ones that change what she
        believes about the conversation: a message that failed to send, one
        whose delivery is unknown (the group may be looking at a sentence she
        thinks never arrived), and one that was dropped because the
        conversation moved on. That last one matters most: without it she
        believes she answered, stays quiet, and nobody ever hears from her.
        """
        if status == "sent":
            return
        chat_id = int(row["chat_id"])
        topic_id = row["topic_id"]
        try:
            # Idempotent per action_id: `finish_action` already wrote this event
            # in its own transaction for failed/unknown/cancelled, and a status
            # set by `claim_action` (expired) has not. Writing it here covers
            # both without duplicating either.
            self.store.record_action_result(
                action_id=action_id, chat_id=chat_id, topic_id=topic_id,
                status=status, text=str(row["text"]), error=error,
            )
        except Exception as exc:
            # Recording must never take down the sender, but silence about a
            # failure here is how a dropped reply becomes invisible again.
            log.error("could not record the result of action %s (error_type=%s)",
                      action_id, type(exc).__name__)
            return
        log.warning("action result %s action_id=%s status=%s",
                    _label(self.chat_kinds, chat_id, topic_id), action_id, status)
        self.note_activity(chat_id, topic_id)


class _ActionHandler:
    """Bridges model tool calls to durable intent. No Telegram here."""

    def __init__(self, loop: AutonomousLoop, *, chat_id: int, topic_id: int | None,
                 workflow_id: str, observed_through: int,
                 episode_id: str | None = None) -> None:
        self._loop = loop
        self.chat_id = chat_id
        self.topic_id = topic_id
        self.workflow_id = workflow_id
        self.episode_id = episode_id
        self.observed_through = observed_through
        self.actions: list[str] = []
        self.waits: list[str] = []
        self.states: list[str] = []
        # Intent bookkeeping for this turn. Kept separate from `states`: an
        # intent is a goal that survives the turn, `update_state` is a note.
        self.intent_set: str | None = None
        self.intent_resolved: str | None = None
        # The goal `send_message` was told to complete with this message. Kept
        # on the handler too so a turn can report it without re-reading storage.
        self.pending_intent_resolution: str | None = None
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
        if name == "set_intent":
            return self._set_intent(arguments)
        if name == "resolve_intent":
            return self._resolve_intent(arguments)
        return ({"status": "rejected", "reason": "unknown_tool",
                 "executed": False}, "rejected")

    def _send_message(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        action_id = _new_id("act")
        freshness = str(arguments.get("freshness") or "default")
        cap = int(arguments.get("max_unobserved_events") or 0)
        if freshness == "default":
            # Adapt to the pace of the room instead of demanding a frozen one.
            #
            # `strict` means "cancel if anything new arrived". In a group that
            # produces a message every few seconds while the model is thinking
            # for a minute, that cancels essentially every reply - measured
            # live: two speak decisions, both cancelled, zero delivered. But
            # making the default "send anyway" would let a reply land after the
            # question was already answered, which is worse.
            #
            # So a default reply is bounded by TIME, not by a message count. A
            # burst of ten messages in a minute does not make a reply wrong -
            # measured live, 13 arrived where a trailing average predicted 4,
            # and a count-based guess dropped a perfectly current answer. A
            # reply three minutes late is answering a conversation that has
            # moved somewhere else.
            freshness = "bounded"
            cap = 100  # the schema's maximum; time is the real guard
            expires_at = self._loop._clock() + self._loop.default_reply_ttl
        elif freshness == "bounded":
            # A bounded action cannot outlive a sane wall-clock window, but the
            # window must be longer than the longest legitimate wait. The send
            # budget can hold an intent for a full hour; expiring at exactly
            # one hour would drop it just as the budget freed up.
            expires_at = self._loop._clock() + self._loop.bounded_action_ttl
        else:
            expires_at = None
        # Read the goal *before* recording the action: the id is stored with
        # the action so a crash after delivery can still close it. Read after,
        # and a message marked `resolves_intent` would carry nothing.
        intent_to_close: str | None = None
        if arguments.get("resolves_intent") is True:
            current = self._loop.store.current_intent(self.chat_id, self.topic_id)
            if current is not None:
                intent_to_close = str(current["intent_id"])
        self.pending_intent_resolution = intent_to_close
        if intent_to_close is not None and self.episode_id:
            # Tie the goal to the episode that produced it, so "what was I in
            # the middle of" and "what am I trying to do" name one thing.
            self._loop.store.attach_episode_intent(self.episode_id, intent_to_close)
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
            intent_id=intent_to_close,
        )
        self.actions.append(action_id)
        self.outcome = self.outcome or "speak"
        # Closing the goal is conditional on the message actually going out:
        # recording it here would mark the goal done for a reply that may still
        # be discarded as stale, and then nothing would try again.
        return ({"status": "accepted", "action_id": action_id,
                 "executed": False, "delivery": "pending",
                 "resolves_intent": intent_to_close is not None,
                 "detail": ("recorded" if created else "already recorded")}, "accepted")

    def _wait_event(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        wait_id = _new_id("wait")
        expires_at = _seconds_from_iso(arguments.get("expires_at"))
        self._loop.store.add_wait(
            wait_id=wait_id, chat_id=self.chat_id, topic_id=self.topic_id,
            kind="event", purpose=str(arguments["purpose"]),
            event_types=arguments.get("event_types"), expires_at=expires_at,
            episode_id=self.episode_id,
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
            due_at=due_at, expires_at=expires_at, episode_id=self.episode_id,
        )
        self.waits.append(wait_id)
        self.outcome = self.outcome or "waiting"
        return ({"status": "accepted", "wait_id": wait_id,
                 "executed": False}, "accepted")

    def _set_intent(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        store = self._loop.store
        goal = str(arguments["goal"]).strip()
        expires_at = _seconds_from_iso(arguments.get("expires_at"))
        intent_id = _new_id("int")
        store.replace_intent(intent_id=intent_id, chat_id=self.chat_id,
                             topic_id=self.topic_id, goal=goal, expires_at=expires_at)
        if self.episode_id:
            store.attach_episode_intent(self.episode_id, intent_id)
        self.intent_set = intent_id
        return ({"status": "accepted", "intent_id": intent_id,
                 "executed": False,
                 "detail": "this goal is shown to you on every later wake"},
                "accepted")

    def _resolve_intent(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        store = self._loop.store
        current = store.current_intent(self.chat_id, self.topic_id)
        if current is None:
            # Nothing open is not an error worth failing a turn over, but it
            # must not be reported as "closed" either.
            return ({"status": "accepted", "executed": False,
                     "detail": "no open goal"}, "accepted")
        status = "abandoned" if arguments.get("abandoned") is True else "resolved"
        closed = store.resolve_intent(
            str(current["intent_id"]),
            how=str(arguments.get("reason") or "agent"), status=status,
        )
        if closed:
            self.intent_resolved = str(current["intent_id"])
        return ({"status": "accepted", "executed": False, "closed": closed,
                 "detail": "goal closed; nobody was told"}, "accepted")

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
