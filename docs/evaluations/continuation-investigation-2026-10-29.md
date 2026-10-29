# Workflow continuation: investigation and paired replay

Status: implemented, and exercised by tests; the paired replay has **not** yet shown it
changing behaviour. This document records the design, what the tests establish, and
why the replay numbers below do not measure the mechanism.

Two corrections to earlier versions of this file:

- An intermediate revision said the prototype "was removed from production for
  Sprint 2" and that its tests were disabled. That was a harness misreading of the
  older stage-one scope: it rolled back code the user had separately authorised on
  2026-10-29. The implementation was restored from the pre-run workspace snapshot;
  the tests are enabled.
- Cache totals were reported as zero. They were not zero — the reader looked for
  `cached_tokens` at the top level of a usage block that nests it under
  `prompt_tokens_details`. After the fix the same replay reports real cache hits.

## Scope

The requested change is a durable logical workflow that may continue across
multiple model activations, retaining the existing application inbox/outbox and
per-chat/topic isolation. A durable goal alone does not establish this lifecycle.
The comparison uses the same recorded group inputs, model and profile for both
arms, with isolated state and a recording transport that never contacts Telegram.

## What was actually wrong

The previous tree persisted an *intent* (a goal) and started a *new workflow* on
every wake. Nothing in the data said "these two activations are the same job", so:

- a restart, a timer firing, or a tool result was indistinguishable from a fresh
  decision task;
- the model saw new observations plus key/value notes and had to guess why it had
  been woken;
- `_close_intent_for_action` was defined but never called, so a goal could outlive
  the message that was supposed to complete it.

## What changed

- `workflow_episodes`: one durable episode identity per scope, adopted rather than
  duplicated by a concurrent opener (partial unique indexes, including the
  NULL-topic case SQLite would otherwise let through).
- `workflows.episode_id` and `agent_waits.episode_id` link activations and waits to
  that identity, with a migration for databases that predate the columns.
- The rendered payload carries `episode: {episode_id, opened_at, activations,
  continuation}`, so "you were woken" and "you were woken to continue" are
  distinguishable facts rather than an inference.
- Closing is narrow: an episode survives only while an open goal or an active wait
  can resume it. Pending observations deliberately do not count - in a busy group
  they always exist, and treating them as a reason would keep one episode open for
  days.
- A goal claimed by `send_message(resolves_intent=true)` closes only after the
  message is really delivered, and the episode is re-settled at that point.
- Startup reports each resumed episode with its activation count; interrupted is
  not reported as fresh.

## Verification

- `tests/test_episode_continuation.py`: 9 tests - timer wake joins the same
  episode, first activation is not a continuation, an episode with nothing to
  resume closes, concurrent openers adopt the winner, one open episode per scope
  under NULL topic, delivery closes the goal and the episode, an episode survives a
  restart, and an old database gains the columns.
- `tests/test_episode_restart_leak.py`: 1 test - a process that dies after delivery
  but before the episode close leaves an open row whose work is already done.
  Before the fix, the next unrelated message in that topic was rendered as a
  continuation of it; startup now re-derives what the episode still owes and closes
  the rest.
- App suite: **406 passed**, against the local Core checkout `5f65658`. Note that
  `pyproject.toml` pins `7618bd0`, so this is not a test of the pinned dependency.
  The earlier claim of "398 passed on a clean venv with the pinned commit" described
  a run of a different tree at a different time; it is not evidence about this one.

## Paired replay

`experiments/paired_replay.py` replays recorded group observations
(`state/fox/bot.sqlite3`, chat `-1004402809405`, opened read-only) into two isolated
databases. The arms differ only in whether episode identity is active; the model,
profile and inputs are identical, and the transport records sends instead of
calling Telegram.

Its report states measured counts for activations, continuations, sends, silence,
waits, goals, prompt size, token usage and elapsed time, plus the explicit limits:
media is replayed by reference and never fetched, non-message observations are
excluded, the reply-to parent id is a stand-in because the recorded payload kept
only a boolean, and naturalness is measured structurally (length, empties) - social
appropriateness still needs a human read. Two arms at one batch size are
indicative; they are not a distribution.

### The replays so far do not exercise continuation

| slice | activations (plain / cont.) | sends | goals set | waits | continuations |
| --- | --- | --- | --- | --- | --- |
| 20 events | 4 / 4 | 2 / 2 | 0 / 0 | 0 / 0 | 0 / 0 |
| 12 events | 2 / 2 | 1 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |

Both arms behaved identically because **neither arm ever had anything to continue**:
the model set no goal and registered no wait. The replayed slice is card-drawing
chatter ("我去", "吓哭了", `/tarot@...` results, "感觉群里人好少") - a conversation with
no task that would need to outlive one activation. A run where the mechanism is
never armed cannot compare it, and the equal counters above are not evidence that
continuation preserves naturalness or reduces disturbance.

Two more measurement notes:

- Cache: the earlier report's `usage_cached: 0` was a reader bug, not a real miss.
  With the fix the same slice reports 6754 cached prompt tokens per arm.
- Elapsed time is dominated by provider latency, not by the arms. One activation in
  the 20-event run took 455 s while its neighbours took 6-21 s; that single call
  produced a 46 s vs 495 s arm difference. Do not read arm timing without checking
  per-activation spans in the journal.

To compare anything, the input has to contain a task. The recorded group has such
stretches - e.g. the message asking for the consulate announcement link, because it
"directly affects a few hundred yuan of budget" - which naturally invite a goal that
waits on someone else's reply.

## Remaining honest limits

- One paired run is not a statistical result; model nondeterminism dominates
  single-sample differences.
- No replay so far has armed the mechanism, so *no behavioural comparison of
  continuation exists yet*. The tests establish the lifecycle; the conversation
  quality question is open.
- Whether continuation *improves* the conversation is a judgement about the
  delivered messages, not something the counters decide. The reply texts are in the
  report for that read.
- Real Telegram delivery, permissions and multi-person social quality remain
  unverified by this work.
- The suite ran against local Core `5f65658`; the pin is `7618bd0`. The unpinned
  commit fixes a malformed-response path (`llm.py::_malformed`) that this app
  depends on being non-fatal, so the pin should be revisited.
