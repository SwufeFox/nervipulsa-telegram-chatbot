# Nervipulsa Telegram Group Agent

The first reference application for [Nervipulsa](https://github.com/SwufeFox/nervipulsa):
a group-chat participant built on the runtime's real event path, not a chat loop
that merely imports it.

The design goal is restraint. The bot is expected to stay quiet most of the time.
Silence is a recorded decision with a reason code, not an empty message.

```
message → dedupe → durable inbox → debounce window → participation policy
        → (only if it will speak) Nervipulsa Runtime → activation → outbox → Telegram
```

## Status

| Milestone | State |
| --- | --- |
| A — offline working slice | **done**, 45/45 checks pass, real Runtime |
| B — live Telegram long polling | **code complete, not exercised against the real API** |
| C — failure injection | **done**, 7 scenarios measured |
| D — runtime feedback | **done**, 1 Core PR + 7 documented gaps |

Read [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md) for exactly what was executed and
what was not.

## Quick start (no credentials)

```bash
python -m pip install -e ".[dev]"

python -m app.main --check      # redacted configuration report
python -m app.main --replay     # the whole pipeline, offline, scripted model
python experiments/milestone_a.py
python experiments/milestone_c.py
python -m pytest -q
```

`--replay` needs no Telegram token and no model key.

## Running live

```bash
export TELEGRAM_BOT_TOKEN="..."          # never in a file
export TELEGRAM_ALLOWED_CHAT_IDS="-1001234567890"   # comma separated
export TELEGRAM_ADMIN_USER_IDS="123456789"
export NERVIPULSA_MODEL="..."
export NERVIPULSA_API_KEY="..."

python -m app.main --dry-run             # full pipeline, never calls sendMessage
python -m app.main                       # live
```

`--dry-run` is the safe way to watch it behave in a real group before it can post.

### Configuration

`config.local.json` (or `--config`) holds only non-secret values:

```json
{
  "bot_name": "Alma",
  "bot_username": "alma_bot",
  "allowed_chat_ids": [-1001234567890],
  "admin_user_ids": [123456789],
  "proactive_enabled": false,
  "debounce_seconds": 3.5,
  "history_messages": 24,
  "proactive_min_interval_seconds": 240,
  "proactive_max_per_hour": 4
}
```

Secrets come from the environment only. A token written into the file is ignored —
`test_config_never_reads_the_token_from_the_file` enforces that. `--check` prints
`"set"`/`"missing"` and never a value.

### Admin commands

Handled before the model sees anything, and only from a configured admin:

| Command | Effect |
| --- | --- |
| `/pause` `/resume` | stop / restart replying |
| `/proactive on|off` | enable or disable unprompted participation |
| `/purge` | delete every stored record for this chat |
| `/stats` `/help` | acknowledged; details go to the log, not the group |

A refusal is silent: announcing "you are not an admin" in a group would disclose
that an admin list exists.

## How it behaves

1. **Addressed → reply.** An `@name` mention or a reply to one of the bot's
   messages is answered with high priority.
2. **Ordinary chat → silence.** Default. No model call, no send.
3. **Bursts collapse.** Messages inside `debounce_seconds` form one turn; three
   quick messages get one answer, not three.
4. **Proactive participation is opt-in.** Off by default, and when on it is scored
   rather than random — see below.
5. **Unsent replies can be revoked.** A newer turn supersedes an older decision
   before the send.
6. **No repeats, no self-loops.** An identical line is dropped; the bot never
   answers its own message or speaks twice in a row without a human in between.
7. **Silence is a row.** `decisions.outcome='silent'` with a reason code. There is
   no code path that sends empty text.
8. **Topic and chat isolation.** A forum topic never sees another topic's history;
   each chat gets its own `Runtime`, transcript, and store scope.
9. **Proactive is rate limited.** Cooldown + hourly cap, both admin-controllable.

### Why the participation score is not a constant

`policy.proactive_score` sums observable terms — is the turn a question, does it
name the bot, does it share vocabulary with the last bot reply, is it unanswered,
is it fresh, is it novel — and subtracts penalties for an already-answered turn, a
very recent bot message, and trivial content (`ok`, `lol`, a bare emoji). No RNG:
the same inputs always give the same score, so a decision can be replayed from its
recorded `detail` block and the tests are not probabilistic.

Only the narrow band around the threshold is offered to a model judge; clearly
above and clearly below are decided locally. That is what keeps an idle group from
costing one request per message.

## Security

- The model is constructed with an **empty tool catalog**. `python_exec` and
  `python_environment` are never offered, and a hallucinated tool call is rejected
  by the runtime with `unknown_tool` and never executed. Asserted against the real
  runtime in `tests/test_security.py`.
- Chat text is **untrusted data**. The system prompt says so; a message that looks
  like a system directive stays a user turn, and a multi-line message cannot forge
  a new speaker.
- The bot **states that it is a bot** in its own profile and never impersonates a
  group member.
- Tokens and model keys never enter the prompt, the store, the logs, or git.
- First version only talks to chats listed in `allowed_chat_ids`. Everything else
  is dropped before any write — including during restart recovery.

## What is guaranteed, and what is not

**Application-owned (tested):** `update_id` dedupe; durable ingest before the poll
cursor advances; recovery of ingested-but-undecided messages on restart; at most one
send attempt per decision; UNKNOWN is terminal and never retried; supersession
before the send; recorded silence; topic/chat isolation; zero tool exposure.

**Nervipulsa-owned:** the event kernel and mailbox admission, the activation ledger,
the transcript and context budgeting, the provider adapter, and the journal.

**Not guaranteed:** exactly-once Telegram delivery; recovery of the runtime's own
transcript or Python namespace; production reliability; multi-process safety. See
[`docs/INTEGRATION_CONTRACT.md`](docs/INTEGRATION_CONTRACT.md).

## Known risk: proactive participation can invent specifics

Measured, not hypothetical. The same scored-0.7 turn ("wait / the migration
script / what about it") drew three different responses from three models:

| model | what it did |
| --- | --- |
| `deepseek-v4.1-flash` | stayed silent |
| `muse-spark-1.3-contributor` | "Hope it's nothing serious." — harmless filler |
| a third model | invented that the migration was supposed to run before the deploy |

The scorer correctly judged the turn *looked* worth joining. It cannot judge what
the model does once it joins — fabricate a premise, emit filler, or say nothing.

Full comparison in [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md) E7b.

Consequences, already applied: proactive stays **off by default**, is capped
hourly, is admin-switchable, and is never used to answer an explicit mention.
If you turn it on, watch the first few hundred turns by hand.

## Layout

```
app/
  bot.py            orchestrator: inbox → turn → decision → runtime → outbox
  policy.py         participation rules and the proactive score
  turns.py          debounce windows per (chat, topic)
  conversation.py   prompt rendering and reply parsing
  agent.py          the only module that touches Nervipulsa
  store.py          SQLite: updates, messages, turns, decisions, sends
  telegram.py       long polling, update parsing, three-valued send outcome
  main.py           CLI entry point
  prompts/group_agent.md   the agent profile (system prompt)
  fixtures.py       synthetic chat events, no real content
experiments/        probe_runtime*.py, milestone_a.py, milestone_c.py
tests/              99 tests
docs/               architecture, gaps, contract, experiments
```

## Documentation

- [`docs/architecture/runtime-integration.md`](docs/architecture/runtime-integration.md)
  — what the runtime actually provides, measured
- [`docs/RUNTIME_GAPS.md`](docs/RUNTIME_GAPS.md) — 8 gaps, evidence, status
- [`docs/INTEGRATION_CONTRACT.md`](docs/INTEGRATION_CONTRACT.md) — pinned commit,
  dependencies, and the guarantees that do not exist
- [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md) — every measurement, and what was
  not run
