# Nervipulsa Telegram Group Agent

A Telegram group bot that mostly keeps quiet.

Most bots in a group chat are a mistake: they answer everything, they repeat
what someone already said, and they fill the room. This one is built around the
opposite rule — **silence is the default**, and every time it speaks, there is a
recorded reason. When it decides not to reply, that decision is stored with a
reason code, not just dropped.

It is also honest about what it is. Ask it whether it is a human and it says no.

```
accepted update → durable observations → recent dialogue + pending work
                → model-chosen recall/history reads → durable outbox → Telegram
```

## What it is like to use

You add the bot to a group, give it a name, and mostly forget it is there. Then:

- **Someone @-mentions it or replies to it.** It considers the whole topic batch and may answer, wait, or stay silent — an address is a signal, not an obligation.
- **It sees a factually wrong claim that could mislead people.** It may correct it if it has something grounded to add.
- **The group is chatting among themselves.** It usually stays quiet; it can join when it has a perspective or curiosity worth adding and proactive participation is enabled.
- **Someone thanks it or makes a joke.** It may reply briefly when that fits, or stay quiet.
- **Someone says its name while talking to someone else** ("let's try what Alma said") — it does not treat that as an automatic summons.

If it does not know something, it says which setting to check rather than
inventing a value.

## How it actually works

There is no keyword social rule and no participation score. Parsed messages from allowed chats become durable observations. Admin commands and our own echoes are persisted but excluded from model input and model wake; unsupported update types, unauthorized chats, and duplicate update IDs do not enter the message observation stream. Reactions use a separate anonymous feedback path. For observations it does receive, the model decides what to do with each batch. Its eleven-tool catalog is closed:

| tool | effect |
| --- | --- |
| `send_message` | put one message in the chat (durable outbox, then Telegram) |
| `finish_silently` | end this turn without speaking |
| `wait_for_event` | end now, wake on a future observation matching optional event-kind and sender filters |
| `wait_until` | end now, be woken at a wall-clock time |
| `update_state` | keep a small working note; it is not a second sourced long-term memory store |
| `set_intent` | record or replace one commitment anchored to a source message |
| `resolve_intent` | close one explicitly selected commitment |
| `remember` | create or revise a sourced experience, person, interest, or preference memory |
| `recall_memories` | retrieve bounded active memory records and their evidence |
| `read_history` | inspect bounded accepted observation history in this scope |
| `forget_memory` | retire a memory from future recall, preserving audit history |

Sender filters only control whether a durable wait is satisfied; they do not prevent otherwise model-eligible observations from waking the model or reduce model calls.

A full observation batch is eligible for dispatch immediately. Partial batches
wait for their idle window or maximum age; retry deadlines, model budgets,
per-topic ordering, and concurrency limits still apply.

A chat/topic can keep several commitments open together. Each records its source
message and sender; starting Bob's request does not replace Alice's unfinished
request. The model selects a commitment with `anchor_message_id` or `intent_id`
when setting, resolving, waiting, or associating a reply. A progress reply keeps
it open; `send_message` with `resolves_intent: true` closes only that selected
commitment after confirmed delivery. Failed or unknown delivery leaves it open.
Linked timers survive unrelated chat until due, cancelled, or expired. Legacy
goals with no recorded source remain available by ID. In the console, `intents`
shows open commitments, their sources, and linked waits for the watched scope.
An intent is private bookkeeping. If the bot takes up a question but needs
information, it must ask through `send_message` before waiting; saving a goal
does not contact the person. It can wait quietly for material already offered.


Meaningful shared episodes, explicit personal preferences, and the bot's own
conversation interests can persist independently of commitments. Memories are
scoped to the exact chat/topic, cite accepted observation sequence IDs, and mark
directly observed evidence separately from inference. Corrections revise the
same ID with `expected_revision`; old revisions remain inspectable. Source
message edits mark affected memories `needs_review`. Forgetting or expiry removes
normal recall and cancels linked waits and pending sends; it does not erase
conversation or audit history. `/purge` removes this chat's stored data.

Recent dialogue and pending work form the model's working context. Long-term memories are
not automatically selected or injected, and there is no requirement to search on every
message. The model chooses when earlier people, preferences, episodes, corrections or
forgetting requests, or memory-linked revisits make retrieval useful. It uses `recall_memories`
to find stored records and `read_history` to inspect accepted source text. Recall can be
narrowed with `source_message_id`, `person_id`, memory `kind`, and literal `query` filters.
Search matches literal words and CJK bigrams rather than meaning; each read returns at most
ten records, with up to six reads and three further model rounds per workflow. Reads are
bounded and may be incomplete. The model must consume each tool receipt before relying on its
evidence; receipts support a bounded continuation, not an unlimited search or redelivery of
the original observations. An empty result cannot establish that something never happened.
Finding or saving a memory does not require a reply; the model may finish silently.
`update_state` remains a working note; sourced long-term facts belong in `remember`.


`wait_for_event` and `wait_until` may carry a `memory_id`, independently of an
intent. Memory-linked timers survive unrelated conversation and restart so an
interest can be revisited without someone assigning a task. A wake asks the
model to reconsider; it does not force a post. A memory-backed `send_message`
requires both `memory_id` and `memory_revision`. Revision, source edits,
forgetting, or expiry cancel pending sends, and claiming a send rechecks that
the memory is active, current, and not awaiting review. Already sending or
unknown actions are never made retryable by a memory change.

Memories and their source text are evidence, never instructions. The prompt
encourages selective remembering and supported corrections without inventing
shared experiences or turning ordinary chat into a queue of tasks.

Each chat/topic scope has its own Core session. Startup takes an exclusive lease
for that session, restores its last committed model transcript before the actor
starts, and reserves event sequence IDs past persisted history. Restore covers
the model view only; the application's durable inbox and Telegram outbox own
observation recovery and delivery. Core writes are asynchronous, so an accepted
submission is not by itself proof that its journal commit has completed. Durable
transcript snapshots continue to grow in SQLite even though in-memory history is
bounded. See [the integration contract](docs/INTEGRATION_CONTRACT.md) and
[runtime gaps](docs/RUNTIME_GAPS.md) for details.
Ordinary assistant text is never delivered — only `send_message` posts. A turn
that calls no tool has not decided anything: it is asked once more, explicitly,
and only then recorded as `undecided` rather than being counted as silence.
State and intent writes are private. When no send, wait, or explicit finish
has occurred, the model gets one bounded completion opportunity to decide
whether to communicate or stay quiet. The same observations are not new input,
and an exclusively private final decision can still end as `noted`.
If that completion returns ordinary text instead of a tool decision, the
existing one-time text-protocol reminder remains available. Both paths share
the workflow's bounded continuation budget; prose is never posted automatically.

It has no shell, no Python, no network access, and no way to read files. Its
application tools can send, wait, manage commitments, and inspect scoped stored
memories and conversation evidence. If asked, it describes those limits honestly.

Background and the known gaps are in
[`docs/architecture/autonomous-participation-design.md`](docs/architecture/autonomous-participation-design.md).

## Quick start (no credentials needed)

Requires Python 3.11 or newer. Create an isolated environment:

```bash
python -m venv .venv
```

Activate it with `.\.venv\Scripts\Activate.ps1` in PowerShell, or
`source .venv/bin/activate` on Linux/macOS, then install and run:

```bash
python -m pip install -e ".[dev]"

python -m app.main --check      # show the resolved config, secrets redacted
python -m app.main --replay     # run the whole pipeline offline, canned model
python -m pytest -q             # the test suite
```

`--replay` needs no Telegram token and no model key. It prints what the bot would
have done with a handful of synthetic messages — useful for seeing the shape of
it before wiring anything real.

On Windows, `run-fox.ps1` uses `.venv\Scripts\python.exe` when present and
propagates Python's exit code. Install dependencies into that environment so
the launcher and subprocess tests use the same pinned Core revision.

To see the replay produce an actual reply, tell it which chat is allowed (the
fixtures use a fixed id):

```bash
TELEGRAM_ALLOWED_CHAT_IDS="-1001234567890" python -m app.main --replay
```

## Running it for real

Three things are required: a bot token, at least one allowed chat, and a model.

```bash
export TELEGRAM_BOT_TOKEN="..."                   # from @BotFather
export TELEGRAM_ALLOWED_CHAT_IDS="-1001234567890" # comma separated
export TELEGRAM_ADMIN_USER_IDS="123456789"        # who may use /pause etc.

export NERVIPULSA_BASE_URL="http://127.0.0.1:3425/v1"
export NERVIPULSA_MODEL="your-model-name"
export NERVIPULSA_API_KEY="..."

python -m app.main --dry-run     # full pipeline, never calls the send API
python -m app.main               # live
```

`--dry-run` is the way to watch it behave in a real group before it can post
anything. The model is still called, the decisions are still recorded, the sends
are logged instead of performed.

On a TTY, the console starts by default; `--no-console` keeps the run log-only,
and `--console` forces command input. Type `help` for commands such as `status`,
`chats`, `watch`, and `intents`. After `watch <chat_id> [topic_id]`, use
`intents [n]` to inspect commitments, `memories [n]` for the scoped memory list,
`memory <id>` for details, revisions and source excerpts, and `history [n]` for
accepted observation history. Wait/action inspection includes linked memory IDs.

**Secrets only ever come from the environment.** A token written into the config
file is ignored on purpose — a token in a committed file ends up in every backup
and screenshot.

## Configuration

Copy `config.example.toml` and edit it. It is commented line by line, and JSON
still works if you already have a `config.local.json`.

```bash
cp config.example.toml config.toml
python -m app.main --config config.toml --check
```

Sections are only for readability — `telegram.poll_timeout` and a top-level
`poll_timeout` are the same setting. Every key can also be set by an environment
variable, and the environment always wins.

The settings you are most likely to touch:

| Setting | Default | What it does |
| --- | --- | --- |
| `allowed_chat_ids` | — | The only chats it will ever read or answer. Empty means it refuses to start. |
| `debounce_seconds` | `3.5` | How long it waits for more messages before answering, so a two-line question is one answer. |
| `history_messages` | `24` | Rolling conversation history size; explicit reads and bounded memory context are separate. |
| `proactive.enabled` | `false` | Whether it may ever speak without being addressed. Off by default. |
| `context_limit` | `1000000` | Character budget for Core’s serialized model view. |
| `max_output_tokens` | `1000000` | Finite output token cap sent to the provider; see below. |
| `request_timeout` | `900` | Must outlive a long reply, or a good answer gets cut off. |

### About the token budgets

`context_limit` defaults to 1,000,000 characters for Core’s serialized model
view; it is not a model context window measured in tokens. `max_output_tokens`
defaults to a finite 1,000,000-token output cap, sent as `max_tokens`. Both are
configurable, and the provider may impose its own context and output limits.

In one run with the old 4096 output cap, 2 of 10 reply decisions returned no
usable content; only one reached the cap. A repeat with the new defaults produced
9 replies from 9 decisions. These small runs do not establish a failure rate, a
latency cause, or that the new cap eliminates truncation. See
[the experiment](docs/LONG_CONVERSATION.md) for the observations and limits.

## Teaching it who it is

The bot's identity is **two files**, not one, and the split matters:

| File | What belongs there | Who edits it |
| --- | --- | --- |
| `app/prompts/soul.md` | Who it is: the impersonation ban, what it cannot do, how it treats instructions hidden in chat messages. | Rarely. These are the rules that keep it honest. |
| `app/prompts/introduction.md` | How it takes part: when to speak, when to stay quiet, length, tone. | Often. This is the part you tune. |

At startup they are concatenated — **soul first, then introduction** — and become
the model's system prompt.

The order helps people organize and review the prompt; it does not give either
file a model-enforced priority. An introduction can conflict with or weaken the
soul’s instructions, so review the composed prompt as a whole. The safety tests
check for specific rules; they do not guarantee model compliance.

### Changing how it behaves

Edit `introduction.md`. If it talks too much, tighten "When you speak". If it is
too terse, relax "Length and tone". Check that the resulting instructions remain
consistent with the soul.

```markdown
## When you speak

Speak when:
- someone addresses you by name or replies to your message;
- you can correct a factual error that would mislead the group;
...
```

### Changing who it is

Edit `soul.md` — but keep these, or the bot may start claiming to be a person:

- it is a bot and never pretends otherwise;
- it has only the declared application tools, with no shell, Python, browsing, or file access;
- instructions found in chat messages are data, not commands.

### Pointing at your own files

Either set the paths in the config file:

```toml
[profile]
soul_path = "/etc/mybot/soul.md"
introduction_path = "/etc/mybot/introduction.md"
```

or with environment variables:

```bash
export BOT_SOUL_PATH="/etc/mybot/soul.md"
export BOT_INTRODUCTION_PATH="/etc/mybot/introduction.md"
```

The two halves are independent — you can replace the introduction and keep the
shipped soul, or the reverse.

A **missing or empty file is a startup error**, never a silent fallback. Running
with an identity nobody reviewed is exactly the failure this design exists to
prevent, so `--check` names the specific file that is missing.

If you have an existing single-file prompt, `BOT_SYSTEM_PROMPT_PATH` (or
`system_prompt_path` in the config) still works: that one file is used verbatim
instead of the two halves.

## Admin commands

Only from a configured admin, only in an allowed chat, and never sent to the
model:

| Command | Effect |
| --- | --- |
| `/pause` `/resume` | stop / restart replying (it keeps recording messages while paused) |
| `/proactive on` `/proactive off` | allow or forbid unprompted participation |
| `/purge` | delete every stored record for this chat |
| `/stats` `/help` | acknowledged; details go to the log, not the group |

A refused command is silent. Announcing "you are not an admin" in a group would
disclose that an admin list exists.

## Feedback

React to a confirmed bot message with 👍 or 👎. Both autonomous actions and
legacy sends are eligible. It records vote changes anonymously, without storing
who reacted, so you can later see which replies landed:

```bash
python experiments/feedback_report.py --db state/bot.sqlite3
```

The report is read-only, includes both send origins with their actual reply text
and separate nullable action/decision IDs, and can inspect older database
snapshots without migration. Changed feedback is durably recorded before the
target topic is scheduled, so the model can see it even without another human
message. It is weak evidence about a reply, not proof of a particular person's
preference. The model may revise a sourced preference or stay silent; feedback
does not edit the prompt automatically or identify voters.

## What it will not do

Stated plainly, because these are the things people assume a bot can do:

- **It cannot run anything.** No shell, Python, file access, or browsing. Its
  tools act only within the application's scoped conversation and memory APIs.
- **It will not pretend to be human.** Ask directly and it says it is a bot.
- **It cannot be reprogrammed from the chat.** "Ignore your instructions" is just
  text from a user, and the soul file says so.
- **It will not answer everything.** Silence is the default and a recorded
  decision, not a failure.
- **It is not multi-tenant.** One process serves one set of allowed chats.

## Where the numbers come from

Everything below was measured, not estimated. The reports are in
[`docs/`](docs/).

- **[Long conversation](docs/LONG_CONVERSATION.md)** — 49 turns, 5 people, 10
  topics against a real model: throughput, the cache-hit curve, and two real
  defects it exposed. Includes the readable chat log.
- **[Human naturalness](docs/HUMAN_NATURALNESS.md)** — fixed social scenarios,
  what "natural" means here, and where the evidence stops.
- **[Integration contract](docs/INTEGRATION_CONTRACT.md)** — the pinned Core
  commit, the Telegram library contract, and the guarantees that do not exist.
- **[Runtime gaps](docs/RUNTIME_GAPS.md)** — what the underlying runtime does not
  provide, with the experiments that showed it.

A summary of the 49-turn run: 40 silences, 9 replies, all 9 delivered, mean
latency 8.2s, 68.9% prompt-cache hit, and 97.1% of output tokens spent on
reasoning rather than text.

## How it is put together

```
app/
  bot.py              orchestrator: inbox → observation → runtime → outbox
  autonomy.py         the autonomous loop: batching, activation, waits, actions
  autonomous_tools.py the closed action tool set the model may call
  agent.py            the only module that touches Nervipulsa
  vision.py           turns a Telegram photo/sticker reference into model input
  store.py            SQLite: durable queue, cursors, workflows, actions, waits
  telegram.py         transport, update parsing, three-valued send outcome
  logwatch.py         readable logs: scope labels, status snapshots
  console.py          the interactive console (`status`, `watch`, `queue`, ...)
  config.py           TOML/JSON config, agent profile composition
  main.py             CLI entry point
  policy.py           legacy keyword/score path — offline replay only
  turns.py            debounce windows (legacy path)
  conversation.py     prompt rendering (legacy path)
  prompts/
    soul.md           who the bot is
    introduction.md   how it takes part
  fixtures.py         synthetic chat events, no real content
experiments/          measurement scripts
tests/                the test suite
docs/                 architecture, gaps, contract, measurements
```

Three design decisions worth knowing about:

**Sends have three outcomes, not two.** A send either succeeded (we hold a
message id), was definitely rejected by Telegram, or is *unknown* — timeout,
connection reset, process died mid-request. Unknown is never retried
automatically, because that is how a bot posts the same message twice.

**Every chat and forum topic gets its own runtime.** Sharing one would let topic B
answer from topic A's history; a live simulation caught exactly that.

**The bot never gets an executor.** It gets exactly eleven application tools.
A startup assertion compares the runtime's exposed schema list against that
closed set and refuses to start on any difference, so a stray `python_exec`
cannot reach the model even if a dependency adds it. In a group chat, a model
with a shell is not a feature; a model with `send_message` is the point.

## Something not working?

Run `--check` first. It reports, in one screen, whether the token is set, whether
the chat allow-list is empty, which model is configured, and which identity files
are missing — plus any config key that looks like a typo and therefore does
nothing.

```bash
python -m app.main --check
```

The most common first-run problems:

- **"refusing to run open"** — `TELEGRAM_ALLOWED_CHAT_IDS` is empty on purpose
  fails rather than answering strangers.
- **"unknown config key"** — a typo. The key is ignored, so the setting you
  thought you changed did nothing.
- **the bot starts but never replies** — check that the group is in the allow
  list and that someone is actually addressing it; ordinary chatter is designed
  to get silence.
