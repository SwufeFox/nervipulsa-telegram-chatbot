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
journal/session identity. Production autonomous scopes expose exactly seven
application tools: `send_message`, `finish_silently`, `wait_for_event`,
`wait_until`, `update_state`, `set_intent`, and `resolve_intent`. Startup checks
the configured catalog and the Runtime's exposed schemas against that closed
set. `python_host=False` ensures these actions cannot grant arbitrary Python
execution. Per-observation handlers are bound and submitted serially within the
scope, then unbound, so a concurrent workflow cannot use another workflow's
handler.

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

