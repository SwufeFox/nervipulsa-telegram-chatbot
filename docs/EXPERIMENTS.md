# Experiments and Measurements

Everything here was executed on this machine. Where something was **not** run, it
says so. Reproduce with the commands given.

Environment: Windows 10, Python 3.13.14, pytest 9.1.1.
Core at `9a734c4` (base `08ead0f`).

---

## E1 — Core baseline (before any change)

```
cd <nervipulsa>
python -m pytest -q
```

```
160 passed in 120.63s (0:02:00)
```

Hosted CI (`.github/workflows/tests.yml`: ubuntu-latest + windows-latest, Python
3.11) was **not** run. A local green suite is not remote verification.

---

## E2 — Runtime probe, pass 1

```
python experiments/probe_runtime.py
```

19 observations, 4 marked FAILS. The failures are *capability* failures, not bugs:

| Probe | Verdict | Detail |
| --- | --- | --- |
| P1 empty `ToolCatalog` removes `python_exec` | WORKS | `actor.tools == []` |
| P1b default catalog tool names | INFO | `['python_exec', 'python_environment']` |
| P2 custom system prompt via constructor | FAILS | prompt was `'You are a helpful software engineer assistant.'`; no keyword exists |
| P3 structured external event types | FAILS | `chat.message` / `external.event` / `telegram.update` → `unknown_route` |
| P3b `user.message` accepts extra fields | INFO | accepted, `event_id` returned |
| P3c what the model sees for a tagged message | INFO | `history` and `chat_id`/`topic_id`/`sender` are **dropped at the projector** |
| P4 cancel / supersede in-flight activation | FAILS | all three event names → `unknown_route` |
| P5 `assistant.message` observable | WORKS | `{"text": "ack", "activation_id": "act_..."}` |
| P5b input event id → activation | WORKS | via `actor.activations` (in-memory list) |
| P6 empty model response | INFO | `actor=paused, paused_reason=response_invalid` |
| P7 transcript restored on restart | FAILS | `new transcript messages=0` |
| P7b durable inspect of old session | INFO | keys: `activations, notice, session_id, tool_receipts, transcript_snapshots` |
| P7c durable sessions listed | INFO | 1 session, 1 activation commit |
| P8 `assistant.message` into a full UI mailbox | INFO | 0 accepted, 300 rejected (bus not started; see E3 Q2 for the real measurement) |
| P9 `ModelRequest.tools` with an empty catalog | INFO | `tools: []`, 3 messages |

---

## E3 — Runtime probe, pass 2

```
python experiments/probe_runtime_2.py
```

**Q1 — does `start()` spawn a Python worker with zero tools?**

```json
{"start_ms": 20, "worker_pid_present": true, "spawn_observed_ms": 139,
 "worker_epoch": 1, "namespace": "current_epoch_only"}
```

Yes. This is gap G2.

**Q2 — is output dropped when the UI mailbox fills?** Bus running, UI consumer not
draining:

```json
{"accepted": 256, "rejected": 44, "ordinary_limit": 256,
 "first_reject_reason": "capacity_exceeded"}
```

Yes, silently. This is gap G5.

**Q3 — what exactly does the provider receive for a tagged message?**

```json
[{"role": "system", "content": "You are a helpful software engineer assistant."},
 {"role": "user", "content": "{\"kind\":\"runtime_status\", ... }"},
 {"role": "user", "content": "hello"}]
```

The `chat_id`, `message_id`, `topic_id`, and `sender` we attached are gone. Chat
identity must be rendered into the message text — this shaped `conversation.py`.

---

## E4 — Core change: configurable system prompt

Contract tests written first:

```
python -m pytest tests/test_agent_profile.py -q
→ 6 failed, 1 passed in 1.76s
```

Six failures were `TypeError: ... unexpected keyword argument 'system_prompt'`; the
seventh (`test_default_system_prompt_is_unchanged`) passed, proving the default was
untouched.

After the change:

```
python -m pytest tests/test_agent_profile.py -q
→ 7 passed in 1.77s

python -m pytest -q
→ 167 passed in 143.96s (0:02:23)
```

One test initially failed on a lifecycle mistake of mine, not a product bug:
`actor.request_stop()` flags the loop and cancels the in-flight request but does not
wake a blocked mailbox wait; the test now closes the mailbox, as `Runtime.shutdown`
effectively does by cancelling its tasks.

---

## E5 — Milestone A: offline working slice

```
python experiments/milestone_a.py
```

**45 checks, 45 passed.** One real runtime, one scripted provider, no network.

```
model calls: 4, activations: 4, telegram sends: 4
```

Behaviour verified:

| Requirement | Check | Result |
| --- | --- | --- |
| ordinary chat → silence | 1a–1e | silent, reason `proactive_disabled`, **0 model calls** |
| @mention → reply | 2a–2d | replied, threaded to the triggering message |
| 3 rapid messages → 1 turn | 3a–3b | one decision, one silent turn |
| 3 rapid addressed messages → 1 reply | 3c–3e | one decision, one send, threaded to the last message |
| open window does not answer early | 4a–4c | no decision, no send, then a reply after the window |
| supersede an unsent reply | 5a–5d | superseded, stale send unclaimed, no stale text sent |
| topics never mix | 6a–6d | topic A only sees A; root scope excludes both |
| unauthorized chat refused | 7a–7c | nothing stored, nothing sent, counted |
| redelivery ignored | 8a | stored once |
| real runtime path | R1–R10 | activations, trace, transcript, journal all real |
| zero tool exposure | R2–R3 | `tools == []`, no `python.requested` ever |
| custom prompt in force | R4–R5 | every request carried it; the coding default never appeared |
| injection is data | 9a–9c | reached the model as a user turn; instructions not pasted |
| proactive gated + rate limited | 10a–10c | score recorded, then `proactive_hourly_cap` |

Counters from that run:

```json
{"updates": 20, "duplicates": 2, "unauthorized": 2, "messages_stored": 16,
 "turns_closed": 10, "replies_decided": 4, "silences_decided": 6,
 "sends_ok": 4, "sends_failed": 0, "sends_unknown": 0, "model_calls": 4,
 "errors": 0}
```

**16 chat messages produced 4 model calls.** Silence is free — the acceptance
criterion "not one LLM request per unrelated message" is measured, not asserted.

Two of the failures in earlier runs of this script were my own test-harness bugs,
recorded here because they are instructive:

- the helper advanced the fake clock *before* ingesting, so the debounce window
  could never elapse;
- the scripted backend scanned the whole prompt, so it "answered" questions that
  were already in the history and the duplicate guard (correctly) dropped the
  second identical reply.

Neither was a product defect. The fix was to make the harness behave like the
production ordering and to have the double read only the post-marker segment.

---

## E6 — Milestone C: failure injection

```
python experiments/milestone_c.py
```

Seven scenarios, real components, real store.

### 1. Duplicate update delivery

```
first ingest stored=1, redeliveries duplicates=6, decisions=1, sends=1, model_calls=1
```

Telegram may redeliver any update; the store dedupes on `update_id` before any
other work, so a redelivery costs one indexed read.

### 2. Crash after the message is stored, before the model call

```
1 message(s) and 0 decision(s) survived the crash;
on restart revived_inbound=1;
after the debounce window decisions=1, sends=1;
runtime transcript carried over=False (new Runtime, new session)
```

This scenario **found a real bug**. The first run of this experiment reported
`revived` as impossible — there was no inbound recovery query, so the message was
durable but never acted on. `Store.unanswered_messages()` and the startup revive
step in `GroupAgent.start()` were added because of this measurement, with
`tests/test_recovery.py` (8 tests) covering it.

The runtime's own transcript is still lost on restart. Recovery restores the *work
item*, not the model's memory.

A second bug surfaced from the recovery test: startup recovery was re-opening
messages from chats that are not on the allow-list, which would have routed around
the authorization gate. Fixed by applying `config.allows_chat` during recovery too.

### 3. Model call timeout

```
agent result status=timeout, text=None; actor phase=working; paused=False;
activations=1; sends=0
```

Two independent timeouts: the application's `NervipulsaAgent._timeout` stops
waiting and returns `status="timeout"`; the runtime's own `ModelBackend` timeout
would later raise `ProviderFailure` and pause the actor. The application does not
wait for the second. **No send is attempted on a timeout**, so a slow model cannot
produce a late reply.

### 4. Crash after the reply is generated, before Telegram is asked

```
row status before crash=pending; after restart pending=['dec_x']; sends made=0
```

`pending` is the only state that is safe to resume: the intent was recorded and the
network was never touched.

### 5. Telegram request sent, response lost

```
unknown=1, sent=0, failed=0, error='read operation timed out', transport attempts=1
pending senders after restart=[], still unknown=1
```

UNKNOWN is terminal. A restart does not requeue it. This is the honest outcome: the
message may be visible in the group, and sending it again would duplicate it.

### 6. New message arrives while a reply is being prepared

```
sends=0, turn outcome=['superseded']
```

The model answered, the decision was superseded mid-flight, and the application
dropped it before the irreversible step. Nervipulsa cannot cancel the activation
(G3).

### 7. Runtime process restart

```
session changed=True; activations before=1; transcript before=2, after=0;
durable sessions listable=1; inspect keys=['activations', 'notice', 'session_id',
'tool_receipts', 'transcript_snapshots']; message rows in store=2; sends=1
```

Nervipulsa persists evidence, not state. The new `Runtime` has a new session id, an
empty transcript, and a fresh worker namespace. `inspect_durable` can read the old
session and its own `notice` says it is inspectable, not resumable. Continuity comes
from the application's chat log.

---

## E7 — Application test suite

```
python -m pytest -q
```

```
99 passed in 23.23s (0:00:23)
```

Distribution and what each file is for:

| File | Tests | Purpose |
| --- | --- | --- |
| `test_security.py` | 13 | tool exposure, injection, redaction, unauthorized chats |
| `test_behavior.py` | 36 | the nine behavioural requirements, scoring, reply parsing |
| `test_durability.py` | 24 | dedupe, cursor ordering, three-valued send state, constraints |
| `test_runtime_path.py` | 7 | proof that the real Runtime path is exercised |
| `test_recovery.py` | 8 | inbound recovery and its authorization gate |
| `test_admin.py` | 12 | pause / proactive / purge, CLI `--check` and `--replay` |

No test sleeps: the clock is injected everywhere it matters. The full suite runs in
~23s, mostly runtime startup and shutdown.

### A correctness note found while reviewing the live loop

The first version of `run_live` `continue`d without polling while paused. That is
wrong: `getUpdates` only returns updates past the offset and Telegram discards
unacknowledged batches, so a paused bot would *lose* messages rather than defer
them. Pausing now keeps ingesting durably and records an `operator_paused` silence
for every turn (`test_admin.py::test_pause_keeps_recording_but_never_replies`).

---

## E8 — Not run

Stated explicitly, because the brief warns against claiming more than was measured:

- **No live Telegram API call.** No bot token was used; `getUpdates`/`sendMessage`
  were exercised only through the recording transport. Milestone B's code path
  exists and is tested at the transport boundary, but has not touched the real API.
- **No real LLM provider.** Every run used `ScriptedBackend`. The
  `OpenAICompatibleBackend` path is exercised only by Core's own tests.
- **No hosted CI run** on either repository for these branches.
- **No multi-process test.** Two processes sharing one SQLite file would both poll;
  nothing prevents it.
- **No long-running soak.** Memory growth over days is unmeasured (G7).

---

## E9 — What this work changed in Core

Exactly one commit, `9a734c4`, adding `system_prompt` to `Transcript`, `LLMActor`,
and `Runtime`. Root cause was a duplicated constant read in three places, so the
fix moved ownership of the value into `Transcript` rather than adding a parameter
that three call sites would each have to remember.

Nothing else in Core was modified. The other seven gaps are documented with
evidence and left open, because each has a correct application-side workaround and
the brief is explicit about not extending the framework without a failure that the
workaround cannot cover.
