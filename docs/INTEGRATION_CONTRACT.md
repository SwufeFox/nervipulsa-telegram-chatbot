# Integration Contract

The cross-repository interface between `nervipulsa-telegram-chatbot` (this repo)
and `nervipulsa` (the runtime). Anything not listed here is not depended on.

## Pinned Core revision

```
repository  https://github.com/SwufeFox/nervipulsa
base        08ead0f   prune: remove workspace fingerprint wiring
branch      feat/configurable-agent-profile
commit      9a734c4   feat: let a host application own its system prompt
```

The application pins a **commit**, never `main`. Before upgrading Core:

1. apply the new commit in the Core working tree;
2. run `python -m pytest -q` in Core (currently 167 passed);
3. run `python -m pytest -q` here (currently 99 passed);
4. run `python experiments/milestone_a.py` and `milestone_c.py` and compare the
   recorded observations in `EXPERIMENTS.md`;
5. update the commit id above and the compatibility table below.

A Core change that breaks any of the five is a breaking contract change and needs
the shared contract updated **before** either side is merged.

## Version compatibility

| Application | Core commit | Core suite | Notes |
| --- | --- | --- | --- |
| 0.1.0 | `9a734c4` | 167 passed / 143.96s | first working integration |

## Depends on (public, stable)

### `nervipulsa.config.Settings`

Fields used: `provider`, `base_url`, `model`, `api_key`, `workspace`,
`context_limit`, `max_timeout`, `default_timeout`, `adapter`.
Constructed directly rather than via `load_settings` so the application owns its own
configuration file and never picks up the user's CLI config by accident.

### `nervipulsa.providers.OpenAICompatibleBackend`

Constructor: `(provider=, base_url=, api_key=, model=, timeout=, adapter=)`.
The application never talks HTTP itself; all provider communication goes through
this adapter.

### `nervipulsa.providers.ScriptedBackend`

Constructor: `(responder: Callable[[ModelRequest], ModelResponse | Awaitable])`.
Used by every test and by `--replay`. `calls` and `requests` are read for
assertions; nothing in production code reads them.

### `nervipulsa.ports`

`ModelRequest` (`messages`, `tools`, `activation_id`, `model`, `purpose`),
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
- **No `python_host` interaction.** The application never emits `python.requested`,
  never reads execution output, and never calls `cancel`/`cancel_running`.
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
- **Not production reliability.** The live path has not been run against the real
  Telegram API or a real provider in this work. See `EXPERIMENTS.md` for exactly
  what was and was not executed.
- **Not multi-tenant.** One process, one authorized chat set, no sharding. Two
  processes on the same SQLite file would both poll; nothing prevents it yet.

## Known contract fragility

| Fragility | Impact | Mitigation |
| --- | --- | --- |
| `actor.activations` is a live list, not an event | a future Core change to its shape breaks `NervipulsaAgent._await_activation` | one function, covered by `test_runtime_path.py`; `RUNTIME_GAPS.md` G6 proposes the event that would replace it |
| `Runtime.start()` spawns a worker regardless of tools | one interpreter per chat | `RUNTIME_GAPS.md` G2 |
| `inspect_durable` payload keys are not typed | diagnostics only | nothing depends on the shape |
| `Delivery.reason` strings are free text | tests assert on `accepted`, not on `reason`, except for `"capacity_exceeded"` | prefer `accepted` in new code |
| `Settings` requires an `api_key` for `openai`/`deepseek`/`openrouter` | a missing key raises `ProviderFailure("request_not_sent")` at the first call, not at startup | `--check` reports it up front |
