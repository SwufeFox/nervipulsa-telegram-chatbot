# Integration Contract

The cross-repository interface between `nervipulsa-telegram-chatbot` (this repo)
and `nervipulsa` (the runtime). Anything not listed here is not depended on.

## Pinned Core revision

The application pins Nervipulsa Core at published `main` commit
`5b0fe9c075379f5ae84d876c9bfd0d43e7d314d9` (see `pyproject.toml`). The pin is a
commit, never a floating branch. Earlier feature-branch SHAs and unpublished
`99284e2` descriptions are historical and do not define this integration.

Before upgrading Core, review its API changes, run the Core and application
checks, then update the pin and this contract. Record actual results in the
version compatibility table only after those checks have completed.

## Version compatibility

| Application | Core commit | Verification |
| --- | --- | --- |
| 0.2.0 working tree | `5b0fe9c075379f5ae84d876c9bfd0d43e7d314d9` | Application: **453 passed** in 672.55s. Core: **300 passed, 5 failed, 17 warnings**. |

These are previously recorded baseline results, not verification of the experience
memory changes described below. Their scripted/integration acceptance and final
results must be recorded after integration; no real-model memory quality claim
is implied by the contract.

The published pin was installed into `.venv` with `pip install -e ".[dev]"`;
subprocess tests use that environment without path injection. Offline replay and
the PowerShell launcher's `-Check` mode both passed.

The real model `main` group simulation completed eight activations with eight
provider usage records, two recorded sends, six silent workflows, and no Python
executions. A separate scripted `run_live` startup/shutdown smoke recorded one
send, returned exit code 0, and closed every Runtime. Both used recording Telegram
transports.

The full Core suite ran on Windows/Python 3.13 in this application's `.venv`.
The observed failures were:

- `test_appcontainer_isolation_boundaries`: the isolated Python process could
  not locate `pyvenv.cfg` and exited 106.
- `test_timeout_and_cancel_kill_the_process_tree` and
  `test_hard_deadline_kills_tool_descendant_and_releases_session_without_replay`:
  descendant processes were still reported alive after cancellation/deadline.
- `test_supervised_cli_reports_one_complete_success` and
  `test_supervised_worker_timeout_allows_replacement_and_completion`: the
  supervised CLI returned `failed` instead of `succeeded`.

These are upstream Python execution/supervision paths. The Telegram application
constructs Core with `python_host=False` and its own action catalog; its integration
results do not establish that Core's Windows executor or isolation paths pass.

Live-polling acceptance uses gated transports and explicit model/send events to
verify durable ingress, independent topics, concurrency limits, and cancellation
cleanup. Its whole-scenario deadlines guard against deadlocks; they do not define
a production startup-latency guarantee.

## Depends on (public, stable)

### `python-telegram-bot` `22.8` (pinned, not the runtime)

The production Telegram transport is PTB's async `Bot`
(`app/telegram.py::PythonTelegramBotTransport`). PTB's async surface moves
between minor versions, and this class uses these calls:

| Our call | PTB call |
| --- | --- |
| `getUpdates` | `Bot.get_updates(...)` → `Update.to_dict()` |
| `sendMessage` | `Bot.send_message(...)` + `ReplyParameters` |
| `getMe` | `Bot.get_me()` |
| `deleteMessage` | `Bot.delete_message(...)` |

PTB is only the HTTP layer. The application owns polling, durable update
acknowledgment, and persistence; it does not use `Application`, `Updater`, or
`telegram.ext` handlers. PTB errors can contain the token-bearing Bot API URL;
`_short()` scrubs it before strings reach logs or the `sends` table.

| PTB exception | `TransportError.kind` | Send outcome |
| --- | --- | --- |
| `BadRequest` (also `Forbidden`, `RetryAfter`) | `rejected` | `failed` |
| `TimedOut`, other `NetworkError` | `indeterminate` | `unknown` |

`BadRequest` subclasses `NetworkError`, so map rejection before the general
network-error case.

### Core settings, providers, and profile

The application constructs `nervipulsa.config.Settings` directly, using its own
configuration instead of loading the CLI user's settings. It uses `provider`,
`base_url`, `model`, `api_key`, `workspace`, `context_limit`,
`max_output_tokens`, `max_timeout`, `default_timeout`, and `adapter`. The app
defaults the serialized-view budget and finite output cap to 1,000,000; provider
limits still apply. `max_timeout` follows `request_timeout` (default 900s).
See `docs/LONG_CONVERSATION.md` for the recorded observations and their limits.

At startup, `Runtime(..., system_prompt=)` receives the application-composed
prompt: `app/prompts/soul.md` followed by `app/prompts/introduction.md`. Core sees
one string; ordering organizes files and does not establish model-enforced
priority. Configured profile files must exist and be non-empty; the introduction
may be omitted. Legacy `BOT_SYSTEM_PROMPT_PATH` uses one file verbatim.

The app uses `OpenAICompatibleBackend(provider=, base_url=, api_key=, model=,
timeout=, adapter=)`. It passes `ModelRequest.max_output_tokens` through the
provider adapter as `max_tokens`; it does not make provider HTTP requests itself.
`ScriptedBackend(responder)` drives Core integration tests and offline probes.
`--replay` uses `ScriptedAgent` on the legacy policy path; it does not exercise
the live Core activation/tool path.

### `nervipulsa.ports` and `nervipulsa.runtime.Runtime`

The public port shapes used here are `ModelRequest` (`messages`, `tools`,
`activation_id`, `model`, `purpose`, optional `max_output_tokens`),
`ModelResponse` (`content`, `tool_calls`, `usage`, `model`), `ToolCall`,
`ProviderFailure(kind, message)`, and `ToolCatalog` (`schemas(*, workspace)`,
`get(name)`).

```python
Runtime(
    settings, backend, workspace,
    *, journal_path=None, echo=None, tool_catalog=None,
    system_prompt=SYSTEM_PROMPT, python_host=True,
    response_commit_hook=None, session_id=None,
)
report = await runtime.start(restore_transcript=False)
runtime.submit_text(text) -> Delivery
runtime.submit_observation(mapping) -> Delivery
runtime.retry() -> Delivery
await runtime.shutdown()
```

For an explicit stable session id, `start()` acquires an exclusive lease for
that journal/session pair. A second active Runtime for the same pair fails to
start. Startup journal work runs off the event loop; it reserves the bus
sequence floor above durable history before new event IDs are allocated. When
`restore_transcript=True`, Core loads the latest committed transcript before
starting the bus and actor, and returns a restore report. Restore recovers only
the committed model view: it does not replay mailbox events, activations,
Python state, or pending effects. The application uses this startup option so
the actor cannot process new input before restoration. Core awaits durable
activation snapshots and tool receipts off the event loop; observational journal
records use a bounded background queue. Successful submission alone does not
prove durability. Application recovery still owns the durable inbox and outbox.

Relevant read surfaces:

| Surface | Contract |
| --- | --- |
| `bus.session_id` | Runtime session identity |
| `actor.activations`, `actor.activation_count` | Bounded recent activation objects and cumulative total |
| `wait_for_activation(...)`, `activation_for_event(...)` | Supported input/output correlation; old evicted lookups may return `None` |
| `agent.activation_finished` | Settle event in recent trace and journal |
| `actor.tools` | Exposed schemas; application checks these against its closed catalog |
| `actor.paused`, `status()` | Pause and runtime diagnostics; status includes `pause_hint` where available |
| `Runtime.trace`, `Runtime.event_count(type=None)` | Bounded recent trace and cumulative observed counts |
| `inspect_durable(...)`, `durable_sessions()` | SQLite inspection; not a restore path |
| activation `context_metrics` | `attempt_count` is cumulative even when retained attempt details are bounded |

Disk commits run asynchronously so actor work does not block on SQLite. Durable
transcript snapshots remain in SQLite and grow over time; in-memory bounds do
not imply a disk retention limit.

### Application scope and tools

`GroupAgent._agent_for` creates one Runtime per `(chat_id, topic_id)` with a
stable session id. Core's session lease prevents concurrent use of the same
journal/session identity. Production autonomous scopes expose exactly eleven
application tools: `send_message`, `finish_silently`, `wait_for_event`,
`wait_until`, `update_state`, `set_intent`, `resolve_intent`, `remember`,
`recall_memories`, `read_history`, and `forget_memory`. Startup checks the configured
catalog and Runtime schemas against that closed set. Unknown tools and Python
remain rejected; `python_host=False` grants no executor. Each Core activation,
including a read continuation, uses the same scope binding and response commit
hook. Handlers are bound and submitted serially, then unbound, so concurrent
workflows cannot use one another's handlers.

Commitments are application records, not separate Core sessions. A scope exposes
`open_intents`, including each commitment's ID, source `anchor_message_id`,
derived `owner_id`, and aggregate episode. `set_intent` and `resolve_intent`
require exactly one source anchor or intent ID. `send_message` and `wait_*` may
select one commitment; omitted selectors mean an independent action or wait.
`resolves_intent: true` requires an explicit selector and takes effect only in
the successful delivery transaction. Replacing one goal gives it a new identity,
so an older in-flight send cannot close its replacement. Legacy goals with no
recorded source remain unanchored and can be selected by ID. These records and
their migration belong to the application SQLite store.

### Sourced experience memory (application contract)

The application owns schema v5 memory records and immutable revision snapshots;
Core does not own or restore them. Migration preserves prior data and generic
`agent_state` notes without inventing provenance or converting them to memories.
Memory never crosses `(chat_id, topic_id)`, including a nullable topic. There are
at most 1,024 active memories per scope; creating at capacity rejects explicitly
without silent eviction, while revision remains possible.

Each JSON-safe record contains `memory_id`, `revision`, scope, `kind`
(`experience`, `person`, `interest`, `preference`), `summary`, `certainty`
(`observed`, `inferred`), importance, person IDs, tags, source sequences and
hydrated sources, status, `needs_review`, and creation/update/expiry times.
Sources carry real sequence/type/message/sender/time metadata and a literal
excerpt of at most 240 characters with a truncation flag. Allowed evidence is
accepted `message`, `message_edit`, `bot_message`, `feedback`, or `action_result`
observations within this scope and no later than the frozen observation bound.
Person IDs must be supported by source authors or authors of same-scope messages
explicitly replied to by a source; anonymous votes never supply a person ID.
`kind=person` requires supported person IDs. Inference is never promoted to
observed fact, and source text remains untrusted data, not instructions.

The model-facing additions and selectors are:

| Tool | Arguments and behavior |
| --- | --- |
| `remember` | Required `summary`, `kind`, `source_sequences`; optional `person_ids`, `tags`, `certainty` (default `inferred`), `importance` (1–5, default 2), `expires_at` (aware ISO timestamp). Omit both `memory_id` and `expected_revision` to create; supply both to revise the same ID. |
| `recall_memories` | Optional `query`, `person_id`, `kind`, `tags`, `memory_id`, `source_message_id`, `limit` (1–10, default 5). Read-only active, unexpired memory retrieval with evidence and review flags. |
| `read_history` | Optional `query`, `person_id`, `message_id`, `through_sequence`, `limit` (1–10, default 5). Read-only accepted conversational/source observations, newest matching selection returned chronologically. |
| `forget_memory` | Required `memory_id`, `expected_revision`; optional `reason`. Retires future recall, retaining conversation and audit history. |
| `send_message` | Optional `memory_id` and `memory_revision` must appear together, independently of intent selectors and `resolves_intent`. |
| `wait_for_event`, `wait_until` | Optional `memory_id`, which may coexist with an intent selector. |

`source_message_id` and `message_id` are positive integer selectors combined
with other filters before limiting. To follow an old `reply_to_message_id`,
recall by its source message ID or read the actual message by `message_id` to
obtain its observation sequence; do not guess a sequence from a Telegram ID.

Argument validation rejects unknown fields, booleans in integer fields, unsupported
sources/people, and stale revisions before mutation. Source lists contain 1–12
unique positive sequences; people and tags are bounded to eight each. Summaries
are bounded to 1,500 characters, IDs to 128, tags to 64, and queries to 256.
Store operations are `remember_memory`, `get_memory`, `recall_memories`,
`memory_history`, `forget_memory`, `expire_memories`, and `read_history`.
Scope and the workflow's `observed_through` are supplied by the application,
never model arguments. Recall applies all supplied filters together before
limiting; literal query terms match summary/tags with OR semantics. History
filters and limits before returning chronological records with actual parsed
payloads. To inspect sequence S, read with `through_sequence=S, limit=1` and
verify the returned sequence rather than assuming a match.

Retrieval uses literal case-folded words and CJK bigrams. Current memories are
filtered and ranked within the bounded collection; history queries use escaped,
parameterized SQLite LIKE. Ranking uses matches, importance, recency and stable
ID, without embeddings or semantic retrieval. Long-term records are retrieved
only when the model calls `recall_memories`; `read_history` retrieves source
events. Ordinary snapshots and tool continuations do not select or inject
long-term memories. Recent dialogue and current work remain the model's working
context. Retrieved items retain uncertainty, revisions, sources and honest
clipping. Missing or empty results do not prove that nothing happened.

Memory tool receipts, including IDs returned by writes, feed back to the **same
workflow** for at most three further inference rounds and six executed reads.
Original observations are consumed once; each application submission increments
`model_calls`, and activation details retain the per-submission metrics. Core
continues to own provider retry/attempt metrics. A terminal action, wait, or finish
prevents an extra continuation. Writes count as noted decisions; read-only loop
exhaustion remains audited `undecided`. Plain prose retains one undecided reminder.
Read tools add no Telegram side effect; a private write can end with `finish_silently`.

`remember_memory` requires the matching current revision to revise an existing
ID. Committed Core mutation calls cache their receipts in scoped
`autonomous_calls` using `get_autonomous_call` / `finish_autonomous_call` in the
same application transaction, so replay cannot create a second revision.
Unknown/foreign IDs and revision conflicts reject explicitly; unjournaled calls
do not invent committed journal rows. Old versions remain available to scoped
operator inspection through `memory_history`.

A memory-linked timer survives unrelated chat and restart, presenting the memory
ID and current validity metadata; the model can recall its contents before deciding.
Updating the same ID keeps the revisit armed.
Forgetting or expiry cancels linked active waits and pending sends atomically.
Source message edits mark dependent memories `needs_review` and cancel pending
sends, retaining waits so the model can reconsider. Revision also cancels pending
sends. `prepare_action` and `claim_action` require the linked memory to be active,
at the selected revision and not awaiting review before `pending → sending`.
Cancelled actions produce durable results. Already `sending` or `unknown` actions
are never cancelled or retried by a memory change. Sending neither resolves nor
forgets the memory; intent completion remains independent. Unused memory expiry
does not itself wake the model. `/purge` removes this chat's records, versions,
links and excerpts; forgetting alone does not promise erasure.

### Live feedback and operator inspection

`feedback_target(chat_id, message_id)` finds confirmed autonomous actions first,
then legacy sends; failed, unknown and foreign targets do not qualify. Reaction
feedback preserves anonymous old/new votes and vote delta without actor identity.
A real change atomically records feedback plus a scoped observation containing
actual target text and separate nullable `action_id` / `decision_id`. No-change
updates create no model observation. The target topic is scheduled only after
durable input/poll commit, even without another human message. Feedback is weak
evidence, not an obligation to post or proof of one person's preferences.

The read-only feedback report covers both origins and inspects older snapshot
columns without migrating them. Console `watch <chat_id> [topic_id]` selects the
scope for `memories [n]`, `memory <id>` (details, revisions, sources), and
`history [n]` (accepted observation history); waits/actions show linked memory
IDs where applicable. Operator inspection does not grant the model file access.


## Application-owned guarantees

Core does not provide these Telegram/application guarantees:

| Guarantee | Where it lives |
| --- | --- |
| `update_id` dedupe | `store.record_update` |
| ingest durable before poll cursor advances | `bot.poll_once` |
| unused ingest recovered on restart | `store.unanswered_messages` + `bot.start` |
| at most one claimed Telegram send per decision | `store.mark_sending` |
| UNKNOWN is terminal and not retried | `store.recover_orphaned_sends` |
| supersession before sending | `bot._reply` |
| no-action/undecided outcomes audited, never empty sends | AutonomousLoop workflow outcome and `observation_skips` |
| topic/chat isolation | Runtime, session id, action catalog, and hook scoped by `(chat_id, topic_id)` |
| closed autonomous tool set; no Python execution | `NervipulsaAgent` catalog and exposed-schema checks |
| cancellation drains owned work before closing storage | `run_live` stops poll/timer/console tasks in `finally`; `GroupAgent.stop` stops dispatchers, drains activations, then closes agents |
| independent commitments and linked waits within one scope | `Store.replace_intent`, per-source uniqueness, scoped selectors, and `agent_intents.episode_id` |
| completion closes only the selected commitment after confirmed delivery | `Store.finish_action` atomically records `sent`, resolves the linked goal when requested, and cancels its waits |

## Guarantees that do not exist

- **Not exactly-once Telegram delivery.** Between `mark_sending` and the
  response, the outcome can be UNKNOWN and is never retried automatically.
- **Not full actor recovery.** The application store recovers observations and
  outbound actions; Core restores the last committed model view when configured.
  In-flight activations, mailbox state, and uncommitted Core work are lost, and
  Core and application state transitions are not one transaction.
- **Not an end-to-end production reliability proof.** Integration tests use a
  stub transport and do not establish Telegram delivery or the full failure and
  recovery matrix. See `EXPERIMENTS.md` for recorded lab evidence.
- **Not a verified PTB upgrade path.** PTB is pinned to 22.8 and transport tests
  use a stub Bot; a version bump needs validation against the target API.
- **Not multi-tenant.** One process serves one authorized chat set; polling
  sharding is outside this contract.

## Known contract fragility

| Fragility | Impact | Mitigation |
| --- | --- | --- |
| `inspect_durable` payload keys are not typed | diagnostic shape may change | no application logic depends on the shape |
| `Delivery.reason` strings are free text | reason text may change | prefer `accepted` except for explicitly handled capacity rejection |
| Missing API keys may fail on first provider call | startup alone may not detect it | `--check` reports missing keys |

