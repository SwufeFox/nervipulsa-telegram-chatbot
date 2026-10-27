# Nervipulsa Runtime Integration

Audit of `SwufeFox/nervipulsa` @ `08ead0f` ("prune: remove workspace fingerprint wiring"),
performed 2026-10-27 against the working tree, the test suite, and the CLI source — not
against prior design documents. Every claim below marked **[probe]** was produced by
executing the real `Runtime` (see `experiments/probe_runtime.py` /
`experiments/probe_runtime_2.py`); claims marked **[read]** come from reading source.

Baseline measured on this machine before any change:

```
python -m pytest -q
160 passed in 120.63s
```

Windows 10, Python 3.13.14, pytest 9.1.1. CI (`.github/workflows/tests.yml`) runs the same
suite on `ubuntu-latest` and `windows-latest` with Python 3.11. It was not re-run here; a
local green suite is not remote verification.

---

## 1. What the runtime actually exposes

### Reusable interfaces

| Surface | Signature / shape | Notes |
| --- | --- | --- |
| `Runtime(settings, backend, workspace, *, journal_path, echo, tool_catalog, system_prompt, python_host)` | assembly point | Three seams an embedding app needs: `tool_catalog`, `system_prompt`, `python_host`. The latter two were **added by this application's Core PRs** (see §5). |
| `Runtime.start()` / `await Runtime.shutdown()` | lifecycle | `start()` also spawns a Python worker **[probe Q1]** unless `python_host=False` (see §3 G2 and §5). |
| `Runtime.submit_text(text)` | → `Delivery` | The only text entry point. Emits `user.message` with `{"text": ...}` and nothing else. |
| `Runtime.add_listener(fn)` | observe every accepted event | Called synchronously from the bus observer, after admission. |
| `Runtime.bus.call(source, type, payload, *, reply_to, lane, lane_key, ...)` | → `Delivery` | Direct event injection. Route table is fixed at construction (`Runtime._register`). |
| `Runtime.retry()` | → `Delivery` | Emits `agent.retry`; only meaningful when the actor is paused. |
| `Runtime.status()` | dict | `runtime`, `actor`, `pause_reason`, `context_*`, `journal_*`, `api_key: "set"/"missing"`. |
| `Runtime.inspect_durable(session_id)` / `durable_sessions()` | dict / list | Read-only inspection of a **previous** session's committed activations and transcript snapshots. |
| `LLMActor.transcript` / `.projector` / `.activations` / `.paused` | live objects | Public attributes. `activations[i].input_event_ids` is the only input→output correlation that exists. |
| `Mailbox`, `Bus`, `Lane`, `Emitter` | kernel primitives | Directly constructible; `Bus.register` is immutable once `start()` is called. |

### State ownership (memory vs. persisted)

| State | Where it lives | Survives restart? |
| --- | --- | --- |
| Bus seq / routes / mailboxes | RAM | **No** |
| Transcript (model view) | RAM | **No** **[probe P7]** — a new `Runtime` starts with 0 messages |
| Activations, `held`, tool-call ledger | RAM | **No** |
| Python worker namespace | worker process | **No** — new epoch, `namespace: current_epoch_only` |
| `events` / `deliveries` / `activations` / `executions` / `tool_calls` tables | SQLite (async, bounded queue, WAL+NORMAL) | yes, but **observation** only; nothing reads them back into a session |
| `durable_effects` | SQLite (sync, FULL) | yes; `intent → dispatching → observed`, `unknown` when unconfirmable |
| `durable_activation_commits` / `durable_transcript_snapshots` / `durable_tool_receipts` | SQLite (sync, FULL, BEGIN IMMEDIATE) | yes, but **inspect-only**: `inspect_durable` returns a payload whose own `notice` says inspectable-not-resumable |

`inspect_durable` really does return the old session's data **[probe P7b]**:

```json
{"keys": ["activations", "notice", "session_id", "tool_receipts", "transcript_snapshots"]}
```

`durable_sessions()` lists it **[probe P7c]**:
`[{"session_id": "6511...", "activation_commits": 1, "last_committed_at": 1793087158.39}]`

**Conclusion:** the runtime persists *evidence*, not *state*. There is no resume path. An
application that must survive a restart has to own its own durable store. This is the single
most consequential fact for this application and drives §4.

---

## 2. How an external event would have to enter

`Runtime._register()` installs a fixed route table:

```
user.message → llm        assistant.message → ui
python.requested → python_host          python.started / .finished / .cancel_result → ui / llm
agent.error → ui          agent.feedback / .handler_fired / .retry → llm
session.shutdown → lifecycle           python.environment_changed/_discovered → llm
```

`Bus.call` validates payloads with `_valid_payload`, which whitelists event types. Anything
else is rejected at admission. Measured **[probe P3]**:

```
chat.message     → accepted=false  reason=unknown_route
external.event   → accepted=false  reason=unknown_route
telegram.update  → accepted=false  reason=unknown_route
user.message     → accepted=true
```

So a Telegram update can only enter the runtime as `user.message`, and `user.message`
validation is:

```python
text = data.get("text")
return isinstance(text, str) and text.strip() and len(text.encode()) <= 128 * 1024
```

Extra keys are accepted and the whole payload is snapshotted into the event **[probe P3b:
accepted=true]**, but `Transcript.project()` throws them away **[probe P3c]**:

```
what the provider receives:
[{"role":"system","content":"You are a helpful software engineer assistant."},
 {"role":"user","content":"{\"kind\":\"runtime_status\", ... }"},
 {"role":"user","content":"hello"}]
```

`{"chat_id": -1001, "message_id": 7, "topic_id": 3, "sender": "alice"}` survive in
`Event.payload` and in the journal, and die at the projector. **Chat identity cannot be
carried by the event.** The application must therefore render chat identity into the message
text itself, or hold conversation state entirely on its own side.

There is also a second, unremovable injection: `ContextProjector.status_message()` adds a
`runtime_status` envelope as a `user` message to *every* request, describing worker epoch and
unfinished executions. For a chat bot this is noise about a Python worker it never uses. It
is not configurable.

### Output observation and correlation

`add_listener` receives `assistant.message` with `{"text": ..., "activation_id": ...}`
**[probe P5]**:

```json
[{"text": "ack", "activation_id": "act_70ba11aacd83"}]
```

Correlation back to input is possible only through `Runtime.actor.activations`
**[probe P5b]**:

```json
{"submitted": "evt_00000001",
 "activations": [["act_70ba11aacd83", ["evt_00000001"], "committed"]]}
```

That is a *live in-memory list*, not an event. It is not durable, it grows without bound, and
reading it means reaching into actor internals from application code. There is no
`assistant.message` field that names the input events it answered.

---

## 3. Gaps that matter for this application

### G1 — No configurable system prompt  → **fixed in Core**

Before the fix, all three sites hard-coded `SYSTEM_PROMPT` from `events.py`:
`Transcript.__init__` (size accounting), `ContextProjector.snapshot`, and the compaction
floor in `_maybe_compress`. **[probe P2]**: after constructing a `Runtime` and sending a
message, the provider received
`'You are a helpful software engineer assistant.'` and neither `Runtime.__init__` nor
`LLMActor.__init__` accepted any parameter that could change it.

A group-chat bot cannot assert "I am a software engineer assistant". It must state its own
identity, its silence rules, and the untrusted-input boundary. Resolved — see §5.

### G2 — `start()` always spawns a Python worker · **FIXED-IN-CORE**

**[probe Q1]**, with a tool catalog that exposes zero tools:

```json
{"start_ms": 20, "worker_pid_present": true, "spawn_observed_ms": 139,
 "worker_epoch": 1, "namespace": "current_epoch_only"}
```

`Runtime.start()` called `host.start()` unconditionally, which starts `_scheduler()`, which
calls `_ensure_worker()` before doing anything else. An application that exposes no tools
still paid the spawn, and still had a live arbitrary-Python interpreter bound to its
process, reachable by anything that could call
`bus.call("...", "python.requested", ...)`.

`tool_catalog=EmptyCatalog()` **does** remove `python_exec` from the model's view
**[probe P1]** — `actor.tools == []`, and no `python.requested` event is emitted in a normal
conversation — but that only satisfies "the model cannot call python_exec", not "no Python
execution surface exists".

Measured contrast **[probe P1b]**: the default catalog exposes
`['python_exec', 'python_environment']`.

**Resolved** by Core `6a300ce`: `Runtime(..., python_host=False)` installs a
`NullPythonHost` — no worker, no `python.*` routes, `status()["python"] == "disabled"`.
This application uses it, so the number below is now zero (§6).

### G3 — No cancellation or supersession of an in-flight activation

**[probe P4]**: all three plausible event names are rejected at the route table.

```
activation.cancel → unknown_route
agent.cancel      → unknown_route
event.supersede   → unknown_route
```

`Runtime.cancel()` and `cancel_running()` operate on `python_host` executions only —
`cancel_running()` reads `host.active_execution_facts()`, which is `None` when no Python is
running. `LLMActor.request_stop()` stops the whole actor, not one activation.

Requirement 5 of the brief ("a new message may revoke an unsent old reply") therefore cannot
be implemented as runtime cancellation. The application must implement supersession itself:
drop the decision before sending, and never send it afterwards. That is a legitimate and
arguably better design — the send is the only irreversible act — but it must be stated
plainly rather than described as runtime cancellation.

### G4 — Empty model output is an error, not silence

**[probe P6]**: a model returning `content=""` with no tool calls produces

```json
{"actor": "paused", "paused_reason": "response_invalid",
 "events": ["user.message", "agent.error"]}
```

`_validate()` returns `["empty"]`, and `_complete_and_commit` pauses the actor. A chat agent
whose correct answer is often "say nothing" cannot express that through model output: a
silent model stalls the whole runtime until `retry()`. **Silence must be decided outside the
model**, which is why this application treats silence as an application decision
(`Decision.silent()`) and never asks the model to reply with nothing.

### G5 — Output is dropped silently when the UI mailbox is full

**[probe Q2]**, bus running, UI consumer not draining:

```json
{"accepted": 256, "rejected": 44, "ordinary_limit": 256,
 "first_reject_reason": "capacity_exceeded"}
```

Accepted output is fine; the 44 dropped `assistant.message` events are dropped with only a
`Delivery(accepted=False, reason="capacity_exceeded")` on the delivery sink. The actor does
not learn that its reply was lost. For a chat bot, a lost reply is a lost user-visible
action. This application does **not** rely on `assistant.message` for delivery — it derives
the reply from a later, application-owned read of the same activation — so the failure mode
is "no reply" rather than "wrong reply", but it is still a silent loss and is recorded in
`docs/RUNTIME_GAPS.md`.

### G6 — `ModelRequest.tools == []` is still sent, and there is no `tools` omission

**[probe P9]**: `{"tools": [], "messages_count": 3}`. `providers.py` only attaches `tools`
when the list is truthy, so `[]` is harmless on the wire. Minor, but worth knowing: an empty
catalog does not produce a malformed request.

---

## 4. Integration design chosen for this application

Given §3, the boundary is:

```
Telegram update
   ↓  (app) authorize → dedupe on update_id → persist inbound row
   ↓  (app) debounce into a Turn
   ↓  (app) ParticipationPolicy: deterministic rules, then optional model judge
   ↓  (app) build a single prompt string
Runtime.submit_text(prompt)          ← real Nervipulsa path
   ↓  LLMActor → ModelBackend → ModelResponse
Runtime.add_listener → assistant.message + activation_id
   ↓  (app) Decision(decision_id) persisted with status=PENDING, then SENDING
   ↓  (app) Telegram sendMessage → message_id  |  UNKNOWN
```

**What stays in Core:** event kernel, mailbox admission/backpressure, activation ledger,
transcript + context budgeting, provider adapter, journal.

**What stays in the application:** chat identity, conversation grouping, participation
decisions, silence, supersession, durable inbox/outbox, Telegram I/O, admin controls.

**The application does not:**
- monkey-patch `LLMActor`, `Transcript`, or `Bus`;
- read `actor.transcript` to build its own conversation history (that is the runtime's model
  view, not the chat log);
- treat `assistant.message` acceptance as "sent to Telegram";
- treat `python.finished.effect_state == "observed"` as any statement about Telegram.

A single prompt string per turn carries everything the model needs. The application formats
it as:

```
[chat -1001234567890 topic 3]
alice: is the deploy done?
bob: yes, five minutes ago
alma (you, a bot): thanks for confirming.
--- new messages ---
carol: great
```

The bot's own name is in every line so the model cannot drift into impersonating a member,
and the system prompt states that participant text is untrusted data.

**One runtime session per authorized chat.** Separate chats never share a `Runtime`,
transcript, or prompt — the strongest possible isolation given that `Transcript` is
append-only and unbounded for the life of a session.

---

## 5. Core change made because of this audit

**PR: `feat/configurable-agent-profile`** — `system_prompt` keyword on `Transcript`,
`LLMActor`, and `Runtime`.

- `tests/test_agent_profile.py` (7 tests) written first; 6 failed with `TypeError:
  unexpected keyword argument 'system_prompt'`, the 7th (default unchanged) passed.
- Implementation: `Transcript` owns `system_prompt`; `_system_message()` is the single source
  for `snapshot()`, the size accounting baseline, `ContextProjector.snapshot()`, and the
  compaction floor. Previously those three sites each re-read the module constant, so a
  custom prompt would have made `serialized_length()` and the compacted view disagree.
- Default is unchanged: `Transcript()` still yields
  `SYSTEM_PROMPT`. Existing callers and all 160 existing tests are unaffected.
- Full suite after the change: see §6.

Why this is generic, not Telegram-specific: any embedding application — a chat participant,
a ticketing agent, a scheduler — needs to state who it is. The default prompt asserts a
coding-CLI identity that is wrong for every one of them. Nothing in the change mentions a
chat platform.

**PR 2: `python_host=False`** — `Runtime(..., python_host: bool = True)`, Core `6a300ce`.

- `tests/test_python_host_optional.py` (7 tests) written first; 6 failed with `TypeError:
  unexpected keyword argument 'python_host'`, the 7th (default still spawns) passed.
- A `NullPythonHost` keeps every attribute `Runtime` reads working, so no caller branches
  on whether Python is present. The `python.*` routes are **not registered** when disabled,
  so a submission is refused at admission (`unknown_route`) rather than queued in a mailbox
  whose reader does not exist.
- `status()` reports `python: "disabled"` and `python_enabled: false` — a monitor can tell
  "quiet" from "there is no Python here". (An earlier draft returned `"idle"`; the test was
  corrected, not the code, because `"idle"` would be a lie.)
- Default is unchanged; Core's pre-existing suite passes untouched.

Why this is generic: an application that never grants execution should not carry an
execution process, and "run me a runtime without a Python tool" is a configuration any
non-coding application wants. The evidence for it came from this application (one runtime
per topic × one interpreter per runtime), which is exactly the feedback loop the brief asks
for.

---

## 6. Verification performed

| Check | Command | Result |
| --- | --- | --- |
| Core baseline before changes | `python -m pytest -q` | `160 passed in 120.63s` |
| PR 1 tests before fix | `pytest tests/test_agent_profile.py -q` | `6 failed, 1 passed` (expected) |
| PR 1 tests after fix | same | `7 passed in 1.77s` |
| PR 2 tests before fix | `pytest tests/test_python_host_optional.py -q` | `5 failed, 2 passed` (expected) |
| PR 2 tests after fix | same | `7 passed in 3.58s` |
| Core full suite after both | `python -m pytest -q` | `174 passed in 144.40s` |
| Runtime probes | `experiments/probe_runtime.py`, `probe_runtime_2.py` | 22 observations, in `docs/EXPERIMENTS.md` |
| Worker cost, after PR 2 | `experiments/worker_cost.py` | 10 topics → **0 workers**, 1.0s (was 24.3s) |
| End-to-end case | `experiments/case_topics_workers.py` | 4 topics, 0 cross-topic leaks, 0 workers |

Not verified here: hosted CI on these branches, or any real Telegram API call. A real
provider *was* used (magpie) for the behavioural work; see `docs/EXPERIMENTS.md` E7b/E7d.

## 7. Pinned integration target

```
repository: https://github.com/SwufeFox/nervipulsa
branch:     feat/configurable-agent-profile  (two Core PRs)
base:       08ead0f
commits:    9a734c4  configurable system prompt
            6a300ce  runtime without a Python host
```

The application pins a commit, never `main`. See `docs/INTEGRATION_CONTRACT.md`.
