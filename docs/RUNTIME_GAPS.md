# Runtime Gaps

Unresolved gaps between this application's needs and what Nervipulsa provides.
Each entry names the evidence, whether the gap is generic, the workaround in use,
and what a fix in Core would look like.

Status legend:
- **OPEN** — workaround in the application, no Core change
- **FIXED-IN-CORE** — addressed by a Core PR, with the commit recorded here
- **WONTFIX-IN-CORE** — an application concern; belongs here permanently

---

## G1 — No configurable system prompt · **FIXED-IN-CORE**

**Evidence**
`experiments/probe_runtime.py` P2: after constructing a `Runtime` and submitting a
message, the provider request's system message was
`'You are a helpful software engineer assistant.'`, and neither `Runtime.__init__`
nor `LLMActor.__init__` accepted any parameter able to change it.
Three independent read sites: `Transcript.__init__`, `ContextProjector.snapshot`,
`LLMActor._maybe_compress`.

**Failed scenario**
A chat bot cannot tell the model it is a bot in a group chat that usually stays
quiet. It is forced to claim a coding-assistant identity, which is both wrong and a
security problem: the prompt is where the untrusted-input rule lives.

**Generic?** Yes. Any embedding application — chat participant, ticketing agent,
scheduler — needs to state who it is. Nothing about the fix mentions a platform.

**Fix**
Core PR `feat/configurable-agent-profile` (`9a734c4`), branched from `08ead0f`:
`system_prompt` keyword on `Transcript`, `LLMActor`, `Runtime`. `Transcript` owns
the value and `_system_message()` is the single source for all three read sites, so
size accounting, the projected view, and the compaction floor cannot disagree.
Default unchanged; the existing 160 tests still pass (167 total with the 7 new
contract tests).

**Application contract:** pins this commit; see `INTEGRATION_CONTRACT.md`.

---

## G2 — `start()` always spawns a Python worker · **FIXED-IN-CORE**

**Evidence**
`experiments/probe_runtime_2.py` Q1, with a tool catalog exposing zero tools:

```json
{"start_ms": 20, "worker_pid_present": true, "spawn_observed_ms": 139,
 "worker_epoch": 1, "namespace": "current_epoch_only"}
```

`Runtime.start()` calls `host.start()` unconditionally; `PythonHost._scheduler()`
calls `_ensure_worker()` before its loop.

**Failed scenario**
This bot exposes no tools at all. It still pays a worker spawn per chat (one
`Runtime` per authorized chat), still holds a live arbitrary-Python interpreter in
its process, and still scans for a Python interpreter at startup. On a host without
a usable interpreter, `start()` fails for a bot that will never execute Python.

**Generic?** Yes. "Build me a runtime with no Python tool" is a legitimate
configuration for any non-coding application, and the security argument is generic:
an application that never grants execution should not carry an execution process.

**Workaround that was in use (now superseded)**
`EmptyToolCatalog` — zero schemas, `get()` returns `None` — so the model was never
offered a tool and a hallucinated `tool_calls` entry was rejected with
`unknown_tool`. That satisfied "no Python execution permission is exposed" but not
"no execution process exists".

**Escalation evidence**
The per-topic isolation fix (G9) made the cost concrete rather than theoretical:
one runtime per (chat, topic) meant one interpreter per topic. Measured 10 topics
→ 10 live worker processes, never reclaimed while idle, ~300 ms each.

**Fix**
Core PR `feat/configurable-agent-profile` (`6a300ce`): `Runtime(...,
python_host=False)` installs a `NullPythonHost`. No worker is spawned, the
`python.*` routes are not registered (so a submission is refused with
`unknown_route` instead of queued forever), `status()` reports
`python: "disabled"` plus `python_enabled`, and `runtime_facts()` reports
`namespace: "disabled"`. Default `True`; the pre-existing suite is unchanged.

**Measured after the fix** (`experiments/worker_cost.py`): 10 runtimes → **0
workers**, 1.0 s instead of 24.3 s. The application suite also got faster
(24.8s → 18.3s) because tests stop spawning interpreters.

---

## G3 — No cancellation or supersession of an inactive activation · **OPEN**

**Evidence**
`experiments/probe_runtime.py` P4 — every plausible event type is rejected at the
route table:

```
activation.cancel → unknown_route
agent.cancel      → unknown_route
event.supersede   → unknown_route
```

`Runtime.cancel()` / `cancel_running()` target `python_host` executions only;
`actor.request_stop()` stops the whole actor, not one activation.

**Failed scenario**
Requirement 5: "a new message may revoke an unsent old reply." With a slow
provider, activation A is in flight when a newer message arrives. There is no way
to say "A is stale" to the runtime; A will complete and hand back text.

**Generic?** Yes — any event-driven agent with time-sensitive outputs needs
`event.supersede` semantics. But note the design decision below.

**Workaround in use**
Supersession is application-owned and happens *before the send*, which is the only
irreversible step: `decisions.superseded_by` is checked after the model returns and
before `prepare_send`. A superseded decision is recorded and dropped. The runtime
finishes its activation and its output is discarded by the application. Verified in
`experiments/milestone_c.py` §6 (`sends=0, turn outcome=['superseded']`) and
`tests/test_behavior.py::test_a_reply_generated_while_superseded_is_dropped`.

**Why not a Core PR yet**
The workaround is correct for this application: a reply that is not sent costs a
token budget but no user-visible harm, and dropping it before the side effect is
strictly safer than cancelling a request that may have already produced a
transcript entry. A Core `event.supersede` would save the wasted provider call, not
fix a bug. That is an optimisation, and this application has no measurement saying
the waste matters. Recorded here so the decision is visible rather than implied.

---

## G4 — Empty model output is an error, not silence · **OPEN**

**Evidence**
`experiments/probe_runtime.py` P6: a model returning `content=""` with no tool
calls produced

```json
{"actor": "paused", "paused_reason": "response_invalid",
 "events": ["user.message", "agent.error"]}
```

`LLMActor._validate()` returns `["empty"]`; `_complete_and_commit` pauses.

**Failed scenario**
For a chat participant, "say nothing" is a legitimate and frequent answer. The
runtime has no way to express it: a silent model stalls the actor until an operator
calls `retry()`. In a live group this looks like the bot hanging.

**Generic?** Partially. A coding agent should not return nothing. An event-driven
agent whose output is optional — notifications, monitors, chat — needs a way to
say "handled, no output." That is a real generic distinction (`completed` vs
`completed_with_no_output`), not a Telegram quirk.

**Workaround in use**
Silence is decided entirely *outside* the model. The policy returns
`Decision.silent()` without ever calling the model, and silence is persisted as a
row (`decisions.outcome='silent'`) with a reason code. The model is only invoked
when a reply is expected, so a correctly-behaving model never returns empty. If it
does anyway, the application treats `status == "error"`/no text as
`model_returned_nothing` — recorded, no send, and the turn closes. It does not call
`retry()`.

**Minimal Core interface it would need**
A `ModelResponse` flag (e.g. `declined: bool`) that `_validate` accepts as a
non-error terminal, so `_finish(activation, "committed")` runs and the actor
continues. This application does not need it, because it never asks the model for
silence.

---

## G5 — `assistant.message` is silently dropped when the UI mailbox fills · **OPEN**

**Evidence**
`experiments/probe_runtime_2.py` Q2, bus running, UI consumer not draining:

```json
{"accepted": 256, "rejected": 44, "ordinary_limit": 256,
 "first_reject_reason": "capacity_exceeded"}
```

Accepted output is fine. The rejected `assistant.message` events are dropped with
only a `Delivery(accepted=False)` on the delivery sink; the actor that produced
them is not told.

**Failed scenario**
A user-visible action (a chat reply) is lost with no signal to the component that
produced it. The actor proceeds as if its reply were delivered.

**Generic?** Yes — any application where model output *is* the product, not an
intermediate step. Backpressure on a diagnostic channel is fine (the bounded
diagnostic lane is a deliberate, documented choice); dropping user-facing output
without notifying the producer is the gap.

**Workaround in use**
The application does not treat `assistant.message` as the delivery path. It reads
the reply from the activation it waited on (`agent.ask` returns
`activation.response.content`), so a dropped UI event cannot lose a reply. The cost
is that the application depends on `actor.activations`, a live in-memory list —
see G6.

---

## G6 — Input→output correlation exists only as an in-memory list · **OPEN**

**Evidence**
`experiments/probe_runtime.py` P5/P5b. `assistant.message` carries
`{"text", "activation_id"}` but not the input event ids. The only mapping back to
input is `Runtime.actor.activations[i].input_event_ids`.

**Failed scenario**
An application that needs to know "which message did this reply answer?" must read
actor internals, and the answer does not survive a restart. This is the difference
between an application that can attribute a reply and one that guesses.

**Generic?** Yes. Any application correlating an outbound effect with an inbound
event needs this, and the brief lists "event correlation" as a candidate capability.

**Workaround in use**
`NervipulsaAgent.ask` holds a per-chat lock, snapshots `len(activations)` before
submitting, and then polls for the activation whose `input_event_ids` contains the
`Delivery.event_id` it just got. That is unambiguous because no other activation
can be running on that runtime (one runtime per chat, serialized by the lock).
Verified in `tests/test_runtime_path.py`.

**Minimal Core interface it would need**
Include `input_event_ids` in the `assistant.message` payload (one line in
`_complete_and_commit`), or add a `agent.activation_finished` event carrying
`{activation_id, input_event_ids, status}`. Small and additive. Not submitted:
the polling workaround is correct, and a Core PR should be driven by a failure the
workaround cannot handle.

---

## G7 — `Transcript` is append-only and unbounded for a session's lifetime · **OPEN**

**Evidence**
Read: `llm.py` `Transcript` never drops projections except through model-ordered
compaction at the context limit. `Runtime.trace` and `actor.activations` grow
without bound too (the v0.4 docs list this as an open decision).

**Failed scenario**
A long-lived group chat that keeps one runtime alive forever grows memory without
limit. Mitigated here by *not* keeping one runtime forever (see below), but the
underlying property is real for any durable session.

**Generic?** Yes, but it is a known and documented Core decision, not an oversight.

**Workaround in use**
A `Runtime` is created lazily per chat and its transcript is the model view only;
the application's own `messages` table is the durable chat log, trimmed by
`history_messages` at prompt-build time. Long sessions are bounded by restarting
the process, not by the runtime releasing memory.

---

---

## G8 — Telegram delivery reliability · **WONTFIX-IN-CORE**

Nervipulsa's durable effect protocol covers `python_host` execution
(`intent → dispatching → observed`). It says nothing about `sendMessage`, and it
must not be read as if it did. The application owns a separate outbox with three
terminal states and treats UNKNOWN as terminal. This belongs in the application
permanently: `chat_id` and Telegram error codes must never enter a generic runtime
protocol.

---

## G9 — No way to give an agent per-scope identity without a Runtime per scope · **OPEN**

**Evidence**
`experiments/live_group_sim.py --scenario topics`, first run: asked in forum topic
B "what did we just say about the timeout?", the bot answered from topic A's
transcript.

`GroupAgent` cached its agent by `chat_id`, so every topic in one forum shared a
single `Runtime` and therefore one `Transcript`, which is append-only for the
life of a Runtime. Fixed on the application side by keying on `(chat_id,
topic_id)`.

**Failed scenario**
Any application with a hierarchical conversation — forum topics, threads, email
subjects, ticket sub-conversations — either shares one model view across all of
them, or pays a full `Runtime` (with a Python worker, see G2) per branch.

**Generic?** Yes. "One conversation" is not always one chat, and the runtime gives
an application no way to partition `Transcript` or to tell an activation which
partition it belongs to. This is the same shape as G6: the runtime has exactly one
implicit conversation per session.

**Workaround in use**
One `Runtime` per `(chat_id, topic_id)`. It used to multiply G2 — a forum with
twelve active topics held twelve Python workers — but `python_host=False`
(`6a300ce`) removed that cost: measured **10 topics → 10 runtimes, 0 workers,
1.0 s**. What remains is one `Transcript` object per topic in memory, which is
proportionate to the conversation it holds.

**Minimal Core interface it would still benefit from**
A `partition`/`scope` key on `submit_text` and `Transcript.project`, splitting one
runtime's transcript into independent views. That would collapse N runtimes into
one and share the mailbox and journal. Worth doing when an application needs many
concurrent conversations in one process; not needed for a bot with a handful of
authorized chats, and there is no measured failure to justify it yet.

---
