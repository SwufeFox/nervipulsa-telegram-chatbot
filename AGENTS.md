# Repository Guidelines

## Project Overview

A Python Telegram group agent built on the Nervipulsa runtime. Silence is a normal outcome; only an explicit `send_message` action posts to Telegram. The application keeps durable records of accepted input, workflows, and outbound actions.

## Architecture & Data Flow

Live startup in `app/main.py` loads configuration and the composed prompt, then wires the Telegram client, SQLite store, and `GroupAgent`. The default live path uses `AutonomousLoop`: accepted Telegram updates are authorized and deduplicated, persisted, grouped by `(chat_id, topic_id)`, and presented to a scoped Nervipulsa agent. Its closed action tools record durable effects; accepted sends go through the application's outbox and Telegram transport. Polling and cursor advancement stay application-owned so durable ingest precedes cursor movement.

`app/bot.py` also retains a legacy turn/policy path. `--replay` intentionally uses that path for old offline fixtures; it does not exercise live autonomous tool decisions. `--dry-run` runs the live decision path while suppressing Telegram sends.

## Key Directories

- `app/` — runtime, Telegram integration, configuration, persistence, and orchestration.
- `app/prompts/` — packaged identity and participation prompts; the default profile composes `soul.md` before `introduction.md`.
- `tests/` — pytest unit, integration, and acceptance tests.
- `experiments/` — offline probes and evaluation/replay utilities; not production entry points.
- `docs/` — setup context, experiments, architecture notes, and the Nervipulsa integration contract.
- `tools/` — maintenance utilities. `prune-stuck-budget.py` is deliberately disabled; it is not a database repair command.

## Development Commands

```sh
python -m pip install -e ".[dev]"
python -m pytest -q
python -m app.main --check       # validate and print redacted configuration
python -m app.main --replay      # offline legacy fixture replay; no network
python -m app.main --dry-run     # live decision path, no Telegram sends
python -m app.main              # live polling
```

`run-fox.ps1` provides Windows `-Check` and `-DryRun` launch modes and starts from the repository directory.

## Code Conventions & Common Patterns

- Use typed Python, `snake_case` functions/variables, and `PascalCase` classes. Data transfer records commonly use dataclasses; boundaries use `Protocol` types and injected factories/callables.
- Keep network and runtime operations async. `app.store.Store` is a synchronous SQLite API; preserve its transaction boundaries and locking behavior.
- Inject clocks, transports, and agent adapters where timing or external effects need deterministic control. Keep scopes keyed by `(chat_id, topic_id)`.
- Preserve durable ordering: commit inbound data before advancing the poll cursor, and record outbound intent before making a Telegram request. Keep `failed` and `unknown` send outcomes distinct; unknown sends are not retried automatically.
- Keep the autonomous tool catalog closed. Ordinary model text is not a Telegram message; `send_message` is the explicit send path.
- Keep credentials in environment variables. Configuration diagnostics and logs must not reveal tokens or API keys; use the redacted `--check` output.

## Important Files

- `pyproject.toml` — package metadata, pinned dependencies, console entry point, and pytest settings.
- `app/main.py` — CLI, startup wiring, live polling, replay, and shutdown.
- `app/bot.py` — `GroupAgent` orchestration and ingest ordering.
- `app/autonomy.py`, `app/autonomous_tools.py` — autonomous workflow and its validated action catalog.
- `app/store.py` — SQLite schema, migrations, and durable application state.
- `app/agent.py`, `app/telegram.py` — Nervipulsa adapter and Telegram transport/error mapping.
- `app/config.py` and `app/prompts/` — environment/config loading and system prompt composition.
- `docs/INTEGRATION_CONTRACT.md` — compatibility boundary for Nervipulsa and python-telegram-bot upgrades.

## Runtime/Tooling Preferences

- Python 3.11 or newer; install with pip. Packaging uses setuptools. There is no Node/Bun toolchain.
- `pytest>=8` is the declared development test dependency. The production dependencies pin Nervipulsa to a Git commit and `python-telegram-bot` to `22.8`; preserve those pins and consult `docs/INTEGRATION_CONTRACT.md` before changing either integration.
- No project lint, formatter, type-check, or coverage command is configured in `pyproject.toml`.

## Testing & QA

Run the full suite with `python -m pytest -q`; run a focused module with, for example, `python -m pytest -q tests/test_autonomous_loop.py`. Tests live directly under `tests/` and use shared pytest fixtures, temporary SQLite databases, a manually advanced fake clock, and scripted transports so most cases need no network or real sleeps. `pyproject.toml` treats `DeprecationWarning` as errors and disables pytest's cache provider. No coverage threshold is configured.

When changing the pinned Nervipulsa revision, follow the compatibility and verification process in `docs/INTEGRATION_CONTRACT.md` and update that contract with the supported revision.
