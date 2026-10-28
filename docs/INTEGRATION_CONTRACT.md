# Integration Contract

The cross-repository interface between `nervipulsa-telegram-chatbot` (this repo)
and `nervipulsa` (the runtime). Anything not listed here is not depended on.

## Pinned Core revision

```
repository  https://github.com/SwufeFox/nervipulsa
base        08ead0f   prune: remove workspace fingerprint wiring
branch      feat/configurable-agent-profile
commits     9a734c4   feat: let a host application own its system prompt
            6a300ce   feat: let a runtime run without a Python host
            13c75ec   configurable output-token cap
            1eb558ca3a4c3b1cade8db71bddf41375ae89eb6   fix: preserve retry activation state
            8498fb400d3b53d86da580c93b434bf11df3fa3e   feat: structured observations + pre-tool commit hook
```

Core features consumed by this application:

| Commit | Contract |
| --- | --- |
| `9a734c4` | `Runtime(..., system_prompt=)` / `Transcript(system_prompt=)` |
| `6a300ce` | `Runtime(..., python_host=False)` → `NullPythonHost` |
| `13c75ec` | `Settings.max_output_tokens` / `ModelRequest.max_output_tokens` |
| `1eb558c` | retired `agent.retry` no longer produces a second activation; a recovered activation no longer keeps its stale pause error |
| `8498fb4` | `Runtime.submit_observation(mapping)` admits a bounded structured observation and projects it as a `runtime_event` with its event id; `Runtime(..., response_commit_hook=)` runs after response validation and before recording or dispatching tools, and a raising hook pauses as `response_commit_failed`; observations reset the between-input activation budget while internal events stay capped |

The application pins a **commit**, never `main`. Before upgrading Core:

1. apply the new commit in the Core working tree;
2. run `python -m pytest -q` in Core;
3. run `python -m pytest -q` here;
4. run `python experiments/milestone_a.py` and `milestone_c.py` and compare the
   recorded observations in `EXPERIMENTS.md`;
5. update the commit id above and the compatibility table below.

A Core change that breaks any of the five is a breaking contract change and needs
the shared contract updated **before** either side is merged.

## Version compatibility

| Application | Core commit | Core suite | Notes |
| --- | --- | --- | --- |
| 0.1.0 | `9a734c4` | 167 passed / 143.96s | first working integration |
| 0.1.0 | `6a300ce` | 174 passed / 144.40s | + `python_host=False`; 10 topics → 0 workers |
| historical working tree | based on `6a300ce` | Core 175 passed; app 131 passed | tests imported the sibling Core checkout with the optional output-token extension |
| 0.1.0 | `13c75ec` | 175 passed alongside 3 intermittent Windows failures in one full run | configurable output-token cap; clean dependency install verified 2026-10-28 |
| 0.1.0 | `1eb558ca3a4c3b1cade8db71bddf41375ae89eb6` | 181 passed | fixes retry control-event double activation and stale pause error; verified with the App suite (194 passed) and offline milestones |
| 0.2.0 | `8498fb400d3b53d86da580c93b434bf11df3fa3e` | 187 passed | autonomous participation: structured observations, pre-tool commit hook, observation budget reset; App suite 257 passed |

## Depends on (public, stable)

### `python-telegram-bot` `22.8` (pinned, not the runtime)

The production Telegram transport is PTB's async `Bot`
(`app/telegram.py::PythonTelegramBotTransport`). Pinned exactly, for two reasons:
PTB's async surface moves between minor versions, and this class is written
against the argument names of exactly these four calls.

| Our call | PTB call |
| --- | --- |
| `getUpdates` | `Bot.get_updates(...)` → `Update.to_dict()` |
| `sendMessage` | `Bot.send_message(...)` + `ReplyParameters` |
| `getMe` | `Bot.get_me()` |
| `deleteMessage` | `Bot.delete_message(...)` |

What we deliberately do **not** take from PTB:

- **Not `Application` / `Updater`.** Their polling queue acknowledges updates
  before our durable write commits. `bot.poll_once` still owns the loop and the
  persisted offset, so PTB is only the HTTP layer.
- **Not `telegram.ext`.** No handler registration, no persistence layer; the
  application's SQLite store is the source of truth.
- **Token in errors.** PTB messages can quote the Bot API URL, which contains the
  token. `_short()` scrubs `/bot<id>:<secret>` before any string reaches a log or
  the `sends` table.

Error mapping is part of the contract, because it decides whether a message is
ever retried:

| PTB exception | `TransportError.kind` | Send outcome |
| --- | --- | --- |
| `BadRequest` (also `Forbidden`, `RetryAfter`) | `rejected` | `failed` |
| `TimedOut`, other `NetworkError` | `indeterminate` | `unknown` |

`BadRequest` subclasses `NetworkError` in PTB, so the rejection branch must be
tested first — the reverse order silently converts a fixable refusal into a
never-retried `unknown`.

### `nervipulsa.config.Settings`

Fields used: `provider`, `base_url`, `model`, `api_key`, `workspace`,
`context_limit`, `max_output_tokens`, `max_timeout`, `default_timeout`, `adapter`.
Constructed directly rather than via `load_settings` so the application owns its own
configuration file and never picks up the user's CLI config by accident.

The application defaults `context_limit` to 1,000,000 characters for Core’s
serialized model view, and `max_output_tokens` to a finite 1,000,000-token output
cap. The provider may impose its own limits. `max_timeout` follows
`request_timeout` (default 900s). In the old 4096-cap run, 2 of 10 reply
decisions had no usable content; only one reached the cap. The later run had
9 successful replies from 9 decisions; see `docs/LONG_CONVERSATION.md` for
the evidence and limitations.

### The agent profile is two files

`Runtime(..., system_prompt=)` receives a string the application composes at
startup from `app/prompts/soul.md` (identity, hard limits, untrusted input) and
`app/prompts/introduction.md` (participation rules, tone), in that order. Core sees
one system prompt; the split and ordering organize the files and do not establish
model-enforced priority. The introduction can conflict with or weaken the soul,
so the combined prompt needs review.

Two consequences the contract has to state:

- A missing or empty half raises before the runtime is constructed. There is no
  fallback identity, by design.
- `BOT_SYSTEM_PROMPT_PATH` (legacy) bypasses the split and uses one file verbatim.

### `nervipulsa.providers.OpenAICompatibleBackend`

Constructor: `(provider=, base_url=, api_key=, model=, timeout=, adapter=)`.
When `ModelRequest.max_output_tokens` is set, this adapter sends it as OpenAI-compatible
`max_tokens`; `None` preserves the prior payload. The application never talks HTTP
itself; all provider communication goes through this adapter.

### `nervipulsa.providers.ScriptedBackend`

Constructor: `(responder: Callable[[ModelRequest], ModelResponse | Awaitable])`.
Used by every test and by `--replay`. `calls` and `requests` are read for
assertions; nothing in production code reads them.

### `nervipulsa.ports`

`ModelRequest` (`messages`, `tools`, `activation_id`, `model`, `purpose`, optional
`max_output_tokens`),
`ModelResponse` (`content`, `tool_calls`, `usage`, `model`), `ToolCall`,
`ProviderFailure(kind, message)`, and the `ToolCatalog` shape
(`schemas(*, workspace)`, `get(name)`).

### `nervipulsa.runtime.Runtime`

```python
Runtime(
    settings, backend, workspace,
    *,
    journal_path=None,
    echo=None,
    tool_catalog: ToolCatalog | None = None,
    system_prompt: str = SYSTEM_PROMPT,     # added by 9a734c4
    python_host: bool = True,               # added by 6a300ce
)
await runtime.start()
runtime.submit_text(text) -> Delivery       # Delivery: .accepted, .event_id, .reason
await runtime.shutdown()
```

Read surface the application relies on:

| Attribute | Why | Durability |
| --- | --- | --- |
| `bus.session_id` | diagnostics, restart detection | process only |
| `actor.activations` | reply text + input correlation | process only |
| `actor.tools` | startup assertion that no tool is exposed | derived |
| `actor.transcript` | tests only | process only |
| `actor.paused` | stop waiting instead of burning the timeout | process only |
| `trace` | tests and diagnostics | process only |
| `status()` | diagnostics | derived |
| `inspect_durable(session_id=)` / `durable_sessions()` | post-restart inspection | SQLite |

### `nervipulsa.events`

`SYSTEM_PROMPT` (default only), `Lane`, `Mailbox`, `Delivery`. Tests construct
`Bus`/`Mailbox` directly to exercise `LLMActor` in isolation.

## Does NOT depend on

This is as important as the list above.

- **No monkey-patching.** Nothing in `app/` assigns to an attribute of a Core
  object. The one place a test does this (`test_agent_refuses_to_start_if_tools_
  would_be_exposed`) restores the original in a `finally` and exists to prove the
  startup assertion fires.
- **No private attributes.** Only the names in the tables above.
- **No Core persistence read-back into a session.** `inspect_durable` is called in
  experiments and diagnostics only; it is never used to reconstruct conversation
  state. Its own `notice` says it is inspectable, not resumable.
- **No `python_host` interaction, structurally.** The runtime is built with
  `python_host=False`, so there is no host to interact with: `python.requested`
  is not a registered route, and `status()["python"]` is `"disabled"`. The
  application never emits `python.requested`, never reads execution output, and
  never calls `cancel`/`cancel_running`.
- **No assumption that `assistant.message` is delivered.** It can be dropped when
  the UI mailbox is full (`RUNTIME_GAPS.md` G5); replies are read from the
  activation.
- **No assumption that durable effects cover Telegram.** They cover
  `python_host` execution and nothing else.

## Application-owned guarantees

The runtime does not and must not provide these:

| Guarantee | Where it lives | Test |
| --- | --- | --- |
| `update_id` dedupe | `store.record_update` | `test_durability.py::test_duplicate_update_id_is_ignored` |
| ingest durable before the poll cursor advances | `bot.poll_once` | `test_durability.py::test_cursor_only_advances_after_durable_ingest` |
| unused ingest recovered on restart | `store.unanswered_messages` + `bot.start` | `test_recovery.py::test_startup_revives_an_unanswered_message_and_answers_it` |
| one Telegram send per decision at most | `store.mark_sending` claim | `test_durability.py::test_a_send_cannot_be_claimed_twice` |
| UNKNOWN is terminal and not retried | `store.recover_orphaned_sends` | `test_durability.py::test_unknown_send_is_not_resumed_after_restart` |
| supersession before the send | `bot._reply` | `test_behavior.py::test_a_reply_generated_while_superseded_is_dropped` |
| silence is recorded, never an empty send | `policy.Decision` + `bot._process_turn` | `test_behavior.py::test_silence_is_never_an_empty_message` |
| topic / chat isolation | `store.recent_messages`, `conversation.build_prompt` | `test_behavior.py::test_prompt_for_one_topic_never_contains_another` |
| no tool exposure | `agent.EmptyToolCatalog` + startup assertion | `test_security.py` (4 tests) |

## Guarantees that do not exist

Stated plainly so nobody infers them:

- **Not exactly-once Telegram delivery.** Between `mark_sending` and the response,
  the outcome can be UNKNOWN. It is never retried automatically.
- **Not full crash recovery.** A restart recovers *work items* (unanswered messages,
  pending sends) from the application store. It does not recover the runtime's
  transcript, its activation ledger, or a Python namespace. Conversation memory is
  reconstructed from the stored chat log on the next prompt, which is not the same
  thing as the runtime's own model view.
- **Not production reliability.** A real provider has been exercised (magpie,
  `EXPERIMENTS.md` E7b/E7d); the real Telegram API has not. See `EXPERIMENTS.md`
  E8 for exactly what was and was not executed.
- **Not a verified PTB upgrade path.** `python-telegram-bot` is pinned to `22.8`
  and the transport is unit-tested against a stub `Bot`, not against Telegram.
  A version bump is a code change until `test_ptb_transport.py` and a real
  `getUpdates` round trip both pass.
- **Not multi-tenant.** One process, one authorized chat set, no sharding. Two
  processes on the same SQLite file would both poll; nothing prevents it yet.

## Known contract fragility

| Fragility | Impact | Mitigation |
| --- | --- | --- |
| `actor.activations` is a live list, not an event | a future Core change to its shape breaks `NervipulsaAgent._await_activation` | one function, covered by `test_runtime_path.py`; `RUNTIME_GAPS.md` G6 proposes the event that would replace it |
| ~~`Runtime.start()` spawns a worker regardless of tools~~ | resolved by `6a300ce` | this application passes `python_host=False`; regression-guarded by `test_python_host_optional.py` in Core |
| `inspect_durable` payload keys are not typed | diagnostics only | nothing depends on the shape |
| `Delivery.reason` strings are free text | tests assert on `accepted`, not on `reason`, except for `"capacity_exceeded"` | prefer `accepted` in new code |
| `Settings` requires an `api_key` for `openai`/`deepseek`/`openrouter` | a missing key raises `ProviderFailure("request_not_sent")` at the first call, not at startup | `--check` reports it up front |
