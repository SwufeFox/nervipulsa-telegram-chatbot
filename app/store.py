"""Durable application state: inbound updates, turns, decisions, outbound sends.

The runtime persists evidence of what it was asked and answered, but it cannot
resume a session (docs/architecture/runtime-integration.md §1). Everything that
must survive a process restart therefore lives here, in the application's own
SQLite file:

  updates     one row per accepted Telegram update, keyed by update_id
  messages    the chat log, keyed by (chat_id, message_id)
  turns       one debounce window of chat messages that produced a decision
  decisions   one row per participation decision, including silence
  sends       the outbound side effect log, one row per decision
  cursors     the long-poll offset, advanced only after durable input

Invariants this module enforces, and the tests that check them:

* `record_update` is idempotent on `update_id`. A redelivered update returns
  False and changes nothing.
* A message row and its update row commit together; the poll cursor is a
  separate later step. So "cursor advanced" always implies "input is durable",
  never the reverse.
* A `send` row is written with status PENDING before the Telegram request and
  moved to SENDING, then to SENT (with the returned message id) or UNKNOWN.
  UNKNOWN is terminal: `claim_unknown` never returns an UNKNOWN row for retry.

Telegram identity never leaves this module as anything but an integer or a
string we wrote ourselves.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS updates (
    update_id     INTEGER PRIMARY KEY,
    chat_id       INTEGER,
    message_id    INTEGER,
    received_at   REAL    NOT NULL,
    payload_json  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    topic_id      INTEGER,
    sender_id     INTEGER,
    sender_label  TEXT    NOT NULL,
    is_bot        INTEGER NOT NULL,
    text          TEXT    NOT NULL,
    created_at    REAL    NOT NULL,
    update_id     INTEGER NOT NULL,
    reply_to_bot  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS messages_by_chat ON messages (chat_id, message_id);

CREATE TABLE IF NOT EXISTS turns (
    turn_id       TEXT PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    topic_id      INTEGER,
    first_message INTEGER NOT NULL,
    last_message  INTEGER NOT NULL,
    opened_at     REAL    NOT NULL,
    closed_at     REAL,
    trigger       TEXT    NOT NULL,
    outcome       TEXT
);
CREATE INDEX IF NOT EXISTS turns_open ON turns (chat_id, topic_id, closed_at);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id   TEXT PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    topic_id      INTEGER,
    turn_id       TEXT    NOT NULL,
    outcome       TEXT    NOT NULL CHECK (outcome IN ('reply', 'silent')),
    reason        TEXT    NOT NULL,
    detail_json   TEXT    NOT NULL,
    trigger       TEXT    NOT NULL,
    created_at    REAL    NOT NULL,
    superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS decisions_chat ON decisions (chat_id, created_at);
CREATE INDEX IF NOT EXISTS decisions_outcome ON decisions (chat_id, outcome, created_at);

CREATE TABLE IF NOT EXISTS sends (
    decision_id   TEXT PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    topic_id      INTEGER,
    reply_to      INTEGER,
    text          TEXT    NOT NULL,
    status        TEXT    NOT NULL CHECK (
                      status IN ('pending', 'sending', 'sent', 'failed', 'unknown')
                  ),
    attempts      INTEGER NOT NULL DEFAULT 0,
    telegram_message_id INTEGER,
    error         TEXT,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS sends_status ON sends (status);

CREATE TABLE IF NOT EXISTS observation_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id INTEGER NOT NULL UNIQUE,
    chat_id INTEGER NOT NULL,
    topic_id INTEGER,
    kind TEXT NOT NULL,
    received_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observation_events_scope
    ON observation_events (chat_id, topic_id, sequence);

CREATE TABLE IF NOT EXISTS action_result_events (
    action_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE
);

-- Observations the application generates on the agent's behalf: a timer it
-- asked for coming due, or a turn that failed to decide. Keyed so a retried
-- tick cannot inject the same one twice.
CREATE TABLE IF NOT EXISTS synthetic_observations (
    dedupe_key TEXT PRIMARY KEY,
    sequence   INTEGER NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS autonomous_responses (
    activation_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    topic_id INTEGER,
    envelope_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS autonomous_calls (
    activation_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'committed',
    receipt_json TEXT,
    PRIMARY KEY (activation_id, call_id),
    FOREIGN KEY (activation_id) REFERENCES autonomous_responses(activation_id)
);

-- One logical episode of participation in a scope: the durable identity that
-- outlives any single model activation.
--
-- A `workflows` row is one activation; this row is the work the agent is in the
-- middle of. While an episode is open, every activation for that scope joins
-- it, so a wake after a wait or a tool result is provably the continuation of
-- the same job rather than a fresh decision task. `intent_id` is the model's
-- own statement of the goal, when it made one.
--
-- Deliberately data, not a coroutine: it survives a restart, and a process that
-- dies mid-episode leaves it open so the replayed observations resume it.
CREATE TABLE IF NOT EXISTS workflow_episodes (
    episode_id   TEXT    PRIMARY KEY,
    chat_id      INTEGER NOT NULL,
    topic_id     INTEGER,
    state        TEXT    NOT NULL CHECK (state IN ('open', 'closed')),
    reason       TEXT,
    intent_id    TEXT,
    opened_at    REAL    NOT NULL,
    updated_at   REAL    NOT NULL,
    closed_at    REAL
);
-- At most one open episode per scope, the same NULL-topic caveat as intents:
-- SQLite treats NULLs as distinct, so the second index is required.
CREATE UNIQUE INDEX IF NOT EXISTS workflow_episodes_one_open
    ON workflow_episodes (chat_id, topic_id) WHERE state = 'open';
CREATE UNIQUE INDEX IF NOT EXISTS workflow_episodes_one_open_no_topic
    ON workflow_episodes (chat_id) WHERE state = 'open' AND topic_id IS NULL;
CREATE INDEX IF NOT EXISTS workflow_episodes_scope
    ON workflow_episodes (chat_id, topic_id, opened_at);

-- One row per model activation: a frozen range of observations handed to the
-- agent, plus how that activation ended. Deliberately separate from a Core
-- activation count: one episode may involve several model requests and tool
-- round-trips, and only the workflow owns the event-consumption watermark.
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id     TEXT PRIMARY KEY,
    chat_id         INTEGER NOT NULL,
    topic_id        INTEGER,
    first_sequence  INTEGER NOT NULL,
    last_sequence   INTEGER NOT NULL,
    state           TEXT    NOT NULL CHECK (
                        state IN ('running', 'completed', 'deferred', 'failed')
                    ),
    outcome         TEXT,
    detail_json     TEXT    NOT NULL DEFAULT '{}',
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    -- The logical episode this activation belongs to. Nullable only because
    -- databases written before episodes existed have older rows.
    episode_id      TEXT
);
CREATE INDEX IF NOT EXISTS workflows_scope
    ON workflows (chat_id, topic_id, created_at);
-- `workflows_episode` is created by `_migrate_episode_columns`, not here: on a
-- database that predates episodes the column does not exist yet, and this
-- script runs before the migration.

-- Continuation watermark per (chat, topic). Only advanced once a workflow has
-- reached a terminal state, so a crash replays unprocessed observations.
--
-- `topic_id` is NULL for a non-forum chat, and SQLite treats NULL as distinct
-- from itself in a UNIQUE/PRIMARY KEY. `PRIMARY KEY (chat_id, topic_id)` was
-- therefore not a constraint at all: every upsert inserted a *new* row, the
-- read path returned whichever came first, and the watermark effectively froze
-- at the oldest value. A non-NULL scope_key is the identity instead; it is a
-- stored generated column, so callers keep passing NULL for "no topic".
CREATE TABLE IF NOT EXISTS observation_cursors (
    chat_id        INTEGER NOT NULL,
    topic_id       INTEGER,
    scope_key      TEXT    GENERATED ALWAYS AS (
                       CAST(chat_id AS TEXT) || ':' || COALESCE(CAST(topic_id AS TEXT), '-')
                   ) STORED,
    last_processed INTEGER NOT NULL,
    updated_at     REAL    NOT NULL,
    UNIQUE (scope_key)
);

-- Why an observation never made it into a model request. A silent gap here is
-- indistinguishable from "the agent chose not to react", so it is recorded.
CREATE TABLE IF NOT EXISTS observation_skips (
    sequence      INTEGER PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    topic_id      INTEGER,
    reason        TEXT NOT NULL,
    recorded_at   REAL NOT NULL
);

-- Durable agent-chosen waits. A timer firing is only a reason to look again.
CREATE TABLE IF NOT EXISTS agent_waits (
    wait_id        TEXT PRIMARY KEY,
    chat_id        INTEGER NOT NULL,
    topic_id       INTEGER,
    kind           TEXT    NOT NULL CHECK (kind IN ('event', 'until')),
    purpose        TEXT    NOT NULL,
    event_types_json TEXT,
    due_at         REAL,
    expires_at     REAL,
    status         TEXT    NOT NULL CHECK (
                       status IN ('active', 'cancelled', 'expired', 'satisfied')
                   ),
    created_at     REAL    NOT NULL,
    resolved_at    REAL,
    -- The episode that asked for this wait. A timer firing is a continuation
    -- trigger, so the wait must be attributable to the work it resumes.
    episode_id     TEXT
);
CREATE INDEX IF NOT EXISTS agent_waits_due ON agent_waits (status, due_at);

-- One open intent per scope: "what was I in the middle of".
--
-- A workflow is one model activation; a wait only says when to be woken. Neither
-- carries the reason the agent wanted to come back, so every wake rebuilt a
-- short decision task and the conversation had no thread across activations.
-- This row is that thread. It is deliberately just data - it does not imply a
-- running process, and it survives as long as its scope does.
--
-- `status` is terminal only through an explicit resolution or expiry; a failed
-- workflow does NOT close it, because failing to act is not losing interest.
CREATE TABLE IF NOT EXISTS agent_intents (
    intent_id    TEXT PRIMARY KEY,
    chat_id      INTEGER NOT NULL,
    topic_id     INTEGER,
    goal         TEXT    NOT NULL,
    status       TEXT    NOT NULL CHECK (
                     status IN ('open', 'resolved', 'abandoned')
                 ),
    created_at   REAL    NOT NULL,
    updated_at   REAL    NOT NULL,
    expires_at   REAL,
    resolved_by  TEXT,
    resolved_at  REAL
);
-- At most one *open* intent per scope. A partial index is the constraint:
-- closing an intent must always be possible, opening a second one must not.
CREATE UNIQUE INDEX IF NOT EXISTS agent_intents_one_open
    ON agent_intents (chat_id, topic_id)
    WHERE status = 'open';
-- SQLite treats NULLs as distinct in unique indexes, so the index above alone
-- cannot prevent multiple open intents in a scope without a topic.
CREATE UNIQUE INDEX IF NOT EXISTS agent_intents_one_open_no_topic
    ON agent_intents (chat_id) WHERE status = 'open' AND topic_id IS NULL;

-- Small, agent-authored notes. Source and provenance stay attached; this is
-- not a channel for the model to rewrite its own instructions or permissions.
CREATE TABLE IF NOT EXISTS agent_state (
    chat_id      INTEGER NOT NULL,
    topic_id     INTEGER,
    key          TEXT    NOT NULL,
    value_json   TEXT    NOT NULL,
    source       TEXT    NOT NULL DEFAULT 'agent',
    created_at   REAL    NOT NULL,
    expires_at   REAL,
    PRIMARY KEY (chat_id, topic_id, key)
);

-- Intent before effect. An agent's send_message lands here first; only the
-- outbox worker can turn a row into a Telegram request, and only after an
-- atomic claim that re-checks freshness.
CREATE TABLE IF NOT EXISTS actions (
    action_id       TEXT PRIMARY KEY,
    workflow_id     TEXT,
    chat_id         INTEGER NOT NULL,
    topic_id        INTEGER,
    reply_to        INTEGER,
    text            TEXT    NOT NULL,
    observed_through INTEGER NOT NULL,
    freshness       TEXT    NOT NULL DEFAULT 'strict',
    max_unobserved  INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL CHECK (
                        status IN ('pending', 'sending', 'sent', 'failed',
                                   'unknown', 'cancelled')
                    ),
    attempts        INTEGER NOT NULL DEFAULT 0,
    telegram_message_id INTEGER,
    error           TEXT,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    expires_at      REAL,
    -- The goal this message claims to complete. Written together with the
    -- action so a restart between "Telegram accepted it" and "close the goal"
    -- can still finish the job; without persisting it, an intent would outlive
    -- the message that was supposed to end it.
    intent_id       TEXT
);
CREATE INDEX IF NOT EXISTS actions_status ON actions (status, created_at);
CREATE INDEX IF NOT EXISTS actions_scope ON actions (chat_id, topic_id, status);

CREATE TABLE IF NOT EXISTS feedback_events (
    update_id     INTEGER PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    decision_id   TEXT NOT NULL,
    old_vote      TEXT CHECK (old_vote IN ('up', 'down') OR old_vote IS NULL),
    new_vote      TEXT CHECK (new_vote IN ('up', 'down') OR new_vote IS NULL),
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS feedback_events_decision
    ON feedback_events (decision_id, created_at);
"""


def _now() -> float:
    return time.time()


@dataclass(frozen=True)
class StoredMessage:
    chat_id: int
    message_id: int
    topic_id: int | None
    sender_id: int | None
    sender_label: str
    is_bot: bool
    text: str
    created_at: float
    reply_to_bot: bool = False
    # Attachment classification; empty for plain text. Not stored in the
    # `messages` table (its columns are about conversation, not media), but
    # carried into the observation payload so the agent can tell a sticker
    # from a silent pause.
    media_kind: str = ""
    media_hint: str = ""
    media_file_id: str = ""
    media_preview: str = ""


@dataclass(frozen=True)
class SendRecord:
    decision_id: str
    chat_id: int
    topic_id: int | None
    reply_to: int | None
    text: str
    status: str
    attempts: int
    telegram_message_id: int | None
    error: str | None


class Store:
    """One SQLite file, WAL, explicit transactions. No ORM, no async."""

    def __init__(self, path: Path | str, *, clock: Any = None) -> None:
        self.path = Path(path)
        # Time base. Defaults to wall clock. The application injects the same
        # clock its scheduler uses, so budget windows and stored timestamps
        # cannot disagree (a batch once looked perpetually fresh because the
        # store wrote time.time() while the scheduler read an injected clock).
        # Late-bound to the module function so a caller (or a test harness in
        # another process) that patches `store._now` still controls time.
        self._clock = clock or (lambda: _now())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        # One connection, many coroutines. `sqlite3` connections are not safe
        # to interleave: two topics finishing a turn at the same moment would
        # each open a transaction on the same handle, and the second would fail
        # with "cannot start a transaction within a transaction" - a
        # ProgrammingError that used to be swallowed as a failed pass. The lock
        # is reentrant because helpers like `_record_action_result` are called
        # from inside an existing write.
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate_observation_cursors()
        self._migrate_action_intents()
        self._migrate_episode_columns()
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    # -- lifecycle ---------------------------------------------------------

    def _migrate_action_intents(self) -> None:
        """Add `actions.intent_id` to databases that predate agent intents.

        `CREATE TABLE IF NOT EXISTS` leaves an existing table untouched, so a
        deployed database keeps the old column set and every insert would fail
        on the extra value. Checked against `table_info` rather than catching
        the error, so the intent is explicit and a skipped migration cannot
        look like success.
        """
        columns = {row["name"] for row in
                   self._conn.execute("PRAGMA table_info(actions)").fetchall()}
        if "intent_id" in columns:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("ALTER TABLE actions ADD COLUMN intent_id TEXT")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _migrate_episode_columns(self) -> None:
        """Add the episode links to databases that predate workflow episodes.

        Both columns are nullable and older rows keep NULL, which reads as "this
        work predates episodes" rather than being silently attached to a made-up
        identity. Checked against `table_info` so a skipped migration is visible
        instead of surfacing later as an insert failure.
        """
        wanted = {"workflows": "episode_id", "agent_waits": "episode_id"}
        for table, column in wanted.items():
            columns = {row["name"] for row in
                       self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column in columns:
                continue
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        # Only valid once the column exists, so it cannot live in SCHEMA.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS workflows_episode"
            " ON workflows (episode_id, created_at)")
        self._conn.commit()

    def _migrate_observation_cursors(self) -> None:
        """Atomically rebuild legacy cursors and recover interrupted rebuilds."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            tables = {
                row["name"] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('observation_cursors', 'observation_cursors_old')"
                )
            }
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_xinfo(observation_cursors)")
            }
            sources = []
            if "observation_cursors" in tables and "scope_key" not in columns:
                # Preserve both legacy tables if an earlier migration stopped
                # after renaming its source and another legacy table appeared.
                conn.execute(
                    "CREATE TEMP TABLE cursor_migration_source AS "
                    "SELECT chat_id, topic_id, last_processed, updated_at FROM observation_cursors"
                )
                conn.execute("DROP TABLE observation_cursors")
                tables.remove("observation_cursors")
                sources.append("temp.cursor_migration_source")
            if "observation_cursors" not in tables:
                conn.execute("""CREATE TABLE observation_cursors (
    chat_id        INTEGER NOT NULL,
    topic_id       INTEGER,
    scope_key      TEXT GENERATED ALWAYS AS (
                       CAST(chat_id AS TEXT) || ':' || COALESCE(CAST(topic_id AS TEXT), '-')
                   ) STORED,
    last_processed INTEGER NOT NULL,
    updated_at     REAL NOT NULL,
    UNIQUE (scope_key)
)""")
            if "observation_cursors_old" in tables:
                sources.append("observation_cursors_old")
            for source in sources:
                conn.execute(
                    "INSERT INTO observation_cursors (chat_id, topic_id, last_processed, updated_at)"
                    " SELECT chat_id, topic_id, MAX(last_processed), MAX(updated_at)"
                    f" FROM {source} WHERE 1 GROUP BY chat_id, topic_id"
                    " ON CONFLICT(scope_key) DO UPDATE SET"
                    " last_processed = MAX(observation_cursors.last_processed, excluded.last_processed),"
                    " updated_at = MAX(observation_cursors.updated_at, excluded.updated_at)"
                )
            if "observation_cursors_old" in tables:
                conn.execute("DROP TABLE observation_cursors_old")
            if "temp.cursor_migration_source" in sources:
                conn.execute("DROP TABLE temp.cursor_migration_source")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _now(self) -> float:
        """Current time on this store's injected clock."""
        return float(self._clock())

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _write(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                if immediate:
                    self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def commit_autonomous_response(self, chat_id: int, topic_id: int | None,
                                   activation: Any, response: Any) -> None:
        """Atomically journal the complete validated response before tool dispatch."""
        from dataclasses import asdict

        envelope = json.dumps(asdict(response), ensure_ascii=False, allow_nan=False)
        calls = response.tool_calls
        with self._write() as conn:
            previous = conn.execute(
                "SELECT chat_id, topic_id, envelope_json FROM autonomous_responses WHERE activation_id = ?",
                (activation.id,),
            ).fetchone()
            if previous:
                if (previous["chat_id"], previous["topic_id"], previous["envelope_json"]) != (chat_id, topic_id, envelope):
                    raise ValueError("activation response changed after commit")
                return
            conn.execute(
                "INSERT INTO autonomous_responses VALUES (?, ?, ?, ?, ?)",
                (activation.id, chat_id, topic_id, envelope, self._now()),
            )
            for call in calls:
                conn.execute(
                    "INSERT INTO autonomous_calls (activation_id, call_id, name, arguments_json) VALUES (?, ?, ?, ?)",
                    (activation.id, call.id, call.name,
                     json.dumps(call.arguments, ensure_ascii=False, allow_nan=False)),
                )

    # -- autonomous observation stream -------------------------------------

    def scopes_with_pending_observations(self) -> list[tuple[int, int | None]]:
        """Every (chat, topic) that still has observations past its cursor.

        Used at startup: a restart must re-arm these scopes, not just count
        them, or the backlog is never looked at again.
        """
        rows = self._query(
            "SELECT DISTINCT o.chat_id, o.topic_id FROM observation_events o"
            " LEFT JOIN observation_cursors c"
            "   ON c.chat_id = o.chat_id AND c.topic_id IS o.topic_id"
            " WHERE o.sequence > COALESCE(c.last_processed, 0)"
        )
        return [(int(row["chat_id"]), row["topic_id"]) for row in rows]

    def pending_observations(
        self, chat_id: int, topic_id: int | None, *, limit: int = 64
    ) -> list[sqlite3.Row]:
        """Observations not yet consumed by a terminal workflow, oldest first.

        The cursor is the authority, not the messages table: a message may be
        stored long before the agent looks at it, and an edit arrives as a new
        observation without creating a new message row.
        """
        cursor = self.observation_cursor(chat_id, topic_id)
        if topic_id is None:
            rows = self._query(
                "SELECT * FROM observation_events WHERE chat_id = ? AND topic_id IS NULL"
                " AND sequence > ? ORDER BY sequence LIMIT ?",
                (chat_id, cursor, limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM observation_events WHERE chat_id = ? AND topic_id = ?"
                " AND sequence > ? ORDER BY sequence LIMIT ?",
                (chat_id, topic_id, cursor, limit),
            )
        return rows

    def observation_cursor(self, chat_id: int, topic_id: int | None) -> int:
        """Newest consumed sequence for one scope, or 0.

        MAX, not the first row: databases created before the scope_key fix can
        still hold several rows for one scope (NULL topic_id defeated the old
        primary key), and reading the oldest of those would silently replay a
        backlog the agent has already seen.
        """
        rows = self._query(
            "SELECT MAX(last_processed) AS latest FROM observation_cursors"
            " WHERE chat_id = ? AND topic_id IS ?", (chat_id, topic_id),
        )
        return int(rows[0]["latest"]) if rows and rows[0]["latest"] is not None else 0

    def expire_stale_observations(
        self, *, older_than: float, chat_id: int | None = None,
    ) -> list[tuple[int, int | None, int]]:
        """Advance the cursor past observations too old to answer, per scope.

        Replaying unprocessed observations is right for a restart: a message
        received just before a crash should still be considered. It is wrong for
        a backlog measured in hours. The agent would reason about a conversation
        that ended long ago and then speak into the present one, which is both
        useless and visibly wrong to everyone in the chat.

        Advancing the cursor - rather than deleting rows - keeps the audit trail:
        the observations stay in the table and stop being pending. It returns
        (chat_id, topic_id, skipped_count) so the start-up log can say what was
        dropped instead of dropping it silently.
        """
        cutoff = self._now() - older_than
        with self._write(immediate=True) as conn:
            scopes = conn.execute(
                "SELECT DISTINCT o.chat_id, o.topic_id FROM observation_events o"
                " LEFT JOIN observation_cursors c"
                "   ON c.chat_id = o.chat_id AND c.topic_id IS o.topic_id"
                " WHERE o.sequence > COALESCE(c.last_processed, 0)"
                "   AND o.received_at < ?"
                + (" AND o.chat_id = ?" if chat_id is not None else ""),
                (cutoff, chat_id) if chat_id is not None else (cutoff,),
            ).fetchall()
            skipped: list[tuple[int, int | None, int]] = []
            for scope in scopes:
                cid, tid = int(scope["chat_id"]), scope["topic_id"]
                # Only the stale prefix: a stale message must not pull the cursor
                # past a fresh one that still needs answering.
                newest_stale = conn.execute(
                    "SELECT MAX(sequence) AS seq FROM observation_events"
                    " WHERE chat_id = ? AND topic_id IS ? AND sequence >"
                    "   COALESCE((SELECT MAX(last_processed) FROM observation_cursors"
                    "     WHERE chat_id = ? AND topic_id IS ?), 0)"
                    "   AND received_at < ?",
                    (cid, tid, cid, tid, cutoff),
                ).fetchone()["seq"]
                if newest_stale is None:
                    continue
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM observation_events"
                    " WHERE chat_id = ? AND topic_id IS ? AND sequence >"
                    "   COALESCE((SELECT MAX(last_processed) FROM observation_cursors"
                    "     WHERE chat_id = ? AND topic_id IS ?), 0)"
                    "   AND sequence <= ?",
                    (cid, tid, cid, tid, newest_stale),
                ).fetchone()["n"]
                conn.execute(
                    "INSERT INTO observation_cursors"
                    " (chat_id, topic_id, last_processed, updated_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(scope_key) DO UPDATE SET"
                    "   last_processed = MAX(observation_cursors.last_processed,"
                    "                         excluded.last_processed),"
                    "   updated_at = excluded.updated_at",
                    (cid, tid, int(newest_stale), self._now()),
                )
                skipped.append((cid, tid, int(count)))
        return skipped

    def open_episode(
        self, *, episode_id: str, chat_id: int, topic_id: int | None,
    ) -> sqlite3.Row:
        """Join the scope's open episode, or start one. Returns the winner.

        The whole point is that two activations race here: the partial unique
        index decides, and the loser adopts the row the winner created instead
        of opening a second identity for the same work. Doing this as
        `SELECT`-then-`INSERT` would leave a window where both see nothing open.
        """
        with self._write(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM workflow_episodes WHERE chat_id = ? AND topic_id IS ?"
                " AND state = 'open'", (chat_id, topic_id),
            ).fetchone()
            if existing is not None:
                return existing
            now = self._now()
            conn.execute(
                "INSERT INTO workflow_episodes (episode_id, chat_id, topic_id, state,"
                " opened_at, updated_at) VALUES (?, ?, ?, 'open', ?, ?)",
                (episode_id, chat_id, topic_id, now, now),
            )
            row = conn.execute(
                "SELECT * FROM workflow_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            return row

    def current_episode(self, chat_id: int, topic_id: int | None) -> sqlite3.Row | None:
        """The scope's open episode, if any. What a wake continues."""
        rows = self._query(
            "SELECT * FROM workflow_episodes WHERE chat_id = ? AND topic_id IS ?"
            " AND state = 'open' ORDER BY opened_at LIMIT 1", (chat_id, topic_id),
        )
        return rows[0] if rows else None

    def attach_episode_intent(self, episode_id: str, intent_id: str | None) -> None:
        """Record which goal the open episode has claimed."""
        with self._write() as conn:
            conn.execute(
                "UPDATE workflow_episodes SET intent_id = ?, updated_at = ?"
                " WHERE episode_id = ? AND state = 'open'",
                (intent_id, self._now(), episode_id),
            )

    def close_episode(self, episode_id: str, *, reason: str) -> bool:
        """End the episode. Idempotent; closing twice reports False."""
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE workflow_episodes SET state = 'closed', reason = ?,"
                " closed_at = ?, updated_at = ? WHERE episode_id = ? AND state = 'open'",
                (reason[:200], self._now(), self._now(), episode_id),
            )
            return cursor.rowcount == 1

    def episode_activations(self, episode_id: str) -> list[sqlite3.Row]:
        """Every activation that belonged to one episode, oldest first."""
        return self._query(
            "SELECT * FROM workflows WHERE episode_id = ? ORDER BY created_at",
            (episode_id,),
        )

    def close_stale_episodes(self, *, reason: str = "nothing_owed") -> list[str]:
        """Close open episodes that no longer owe any work.

        Delivery, goal resolution and the episode close are three writes. A
        process that dies between them leaves an `open` episode whose job is
        already finished, and nothing on disk says so - the row looks exactly
        like one still waiting. The facts that distinguish them are durable, so
        they are re-derived here instead of trusted from the row alone: an
        episode stays open only while this scope still has an open goal or an
        active wait that could wake it.

        Used at startup, before any new observation can be attributed to an
        old episode.
        """
        with self._write(immediate=True) as conn:
            rows = list(conn.execute(
                "SELECT e.episode_id FROM workflow_episodes e WHERE e.state = 'open'"
                " AND NOT EXISTS (SELECT 1 FROM agent_intents i"
                "   WHERE i.chat_id = e.chat_id AND i.topic_id IS e.topic_id"
                "   AND i.status = 'open')"
                " AND NOT EXISTS (SELECT 1 FROM agent_waits w"
                "   WHERE w.chat_id = e.chat_id AND w.topic_id IS e.topic_id"
                "   AND w.status = 'active')"))
            now = self._now()
            for row in rows:
                conn.execute(
                    "UPDATE workflow_episodes SET state = 'closed', reason = ?,"
                    " closed_at = ?, updated_at = ? WHERE episode_id = ?"
                    " AND state = 'open'", (reason[:200], now, now, row["episode_id"]))
        return [str(row["episode_id"]) for row in rows]

    def open_episodes(self) -> list[sqlite3.Row]:
        """Episodes still open - the work a restart must resume, not discard."""
        return self._query(
            "SELECT * FROM workflow_episodes WHERE state = 'open' ORDER BY opened_at")

    def open_workflow(
        self, *, chat_id: int, topic_id: int | None,
        first_sequence: int, last_sequence: int, workflow_id: str,
        episode_id: str | None = None,
    ) -> str:
        """Freeze one observation range for the agent. Idempotent on the range."""
        with self._write() as conn:
            existing = conn.execute(
                "SELECT workflow_id FROM workflows WHERE chat_id = ? AND topic_id IS ?"
                " AND state = 'running'",
                (chat_id, topic_id),
            ).fetchone()
            if existing is not None:
                return str(existing["workflow_id"])
            now = self._now()
            conn.execute(
                "INSERT INTO workflows (workflow_id, chat_id, topic_id, first_sequence,"
                " last_sequence, state, detail_json, created_at, updated_at, episode_id)"
                " VALUES (?, ?, ?, ?, ?, 'running', '{}', ?, ?, ?)",
                (workflow_id, chat_id, topic_id, first_sequence, last_sequence, now, now,
                 episode_id),
            )
            return workflow_id

    def running_workflows(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM workflows WHERE state = 'running' ORDER BY created_at")

    def close_workflow(
        self, workflow_id: str, state: str, *,
        outcome: str | None = None, detail: dict[str, Any] | None = None,
    ) -> bool:
        """Terminal transition. Advancing the cursor and closing commit together.

        Nothing before this point moves the watermark, so a crash mid-turn
        replays the same observations instead of silently skipping them.
        """
        if state not in {"completed", "deferred", "failed"}:
            raise ValueError(f"not a terminal workflow state: {state}")
        with self._write() as conn:
            row = conn.execute(
                "SELECT chat_id, topic_id, last_sequence, state FROM workflows"
                " WHERE workflow_id = ?", (workflow_id,),
            ).fetchone()
            if row is None:
                return False
            if row["state"] != "running":
                # Already terminal: replaying recovery must not move the
                # watermark backwards or double-count the turn.
                return False
            conn.execute(
                "UPDATE workflows SET state = ?, outcome = ?, detail_json = ?, updated_at = ?"
                " WHERE workflow_id = ?",
                (state, outcome, json.dumps(detail or {}, ensure_ascii=False), self._now(),
                 workflow_id),
            )
            if state != "completed":
                conn.execute(
                    "UPDATE actions SET status = 'cancelled', error = 'workflow_not_completed', updated_at = ?"
                    " WHERE workflow_id = ? AND status = 'pending'", (self._now(), workflow_id),
                )
            if state == "completed":
                cursor = conn.execute(
                    "SELECT MAX(last_processed) AS latest FROM observation_cursors"
                    " WHERE chat_id = ? AND topic_id IS ?",
                    (row["chat_id"], row["topic_id"]),
                ).fetchone()
                previous = int(cursor["latest"]) if cursor and cursor["latest"] is not None else 0
                if int(row["last_sequence"]) > previous:
                    # Conflict target is scope_key, not (chat_id, topic_id):
                    # a NULL topic_id never conflicts, so the old upsert kept
                    # appending rows and the watermark stopped advancing.
                    conn.execute(
                        "INSERT INTO observation_cursors (chat_id, topic_id, last_processed, updated_at)"
                        " VALUES (?, ?, ?, ?) ON CONFLICT(scope_key)"
                        " DO UPDATE SET last_processed = excluded.last_processed,"
                        " updated_at = excluded.updated_at",
                        (row["chat_id"], row["topic_id"], row["last_sequence"], self._now()),
                    )
            return True

    def record_action_result(
        self, *, action_id: str, chat_id: int, topic_id: int | None,
        status: str, text: str, error: str | None = None,
    ) -> int:
        """Put the outcome of our own send back into the observation stream.

        Before this the result lived only in the `actions` table: the agent saw
        `accepted/pending` when it chose to speak and then never learned
        whether the message arrived. An `unknown` delivery - a timeout where
        Telegram may or may not have accepted it - was completely invisible,
        which is exactly the case where knowing matters most.

        Synthetic update_id: these rows have no Telegram update behind them, so
        a negative monotonic value keeps the UNIQUE constraint satisfied
        without ever colliding with a real (always positive) update id.
        """
        with self._write(immediate=True) as conn:
            return self._record_action_result(conn, action_id=action_id, chat_id=chat_id,
                                              topic_id=topic_id, status=status, text=text, error=error)

    def _record_action_result(self, conn: sqlite3.Connection, *, action_id: str,
                              chat_id: int, topic_id: int | None, status: str,
                              text: str, error: str | None) -> int:
        """Write the one result event for this action, or return the existing one.

        Idempotent on `action_id`, so recovery can rerun it after a crash in
        any of the commit windows without duplicating what the agent sees.
        """
        existing = conn.execute(
            "SELECT sequence FROM action_result_events WHERE action_id = ?", (action_id,)
        ).fetchone()
        if existing:
            return int(existing[0])
        # Adopt a pre-migration event, if present, without emitting it again.
        existing = conn.execute(
            "SELECT sequence FROM observation_events WHERE kind = 'action_result'"
            " AND json_valid(payload_json) AND json_extract(payload_json, '$.action_id') = ?"
            " ORDER BY sequence LIMIT 1", (action_id,)
        ).fetchone()
        if existing:
            sequence = int(existing[0])
        else:
            payload: dict[str, Any] = {"action_id": action_id, "status": status, "text": text[:500]}
            if status == "unknown":
                payload["note"] = "delivery unconfirmed: the message may already be visible in the group. It is not retried."
            elif status == "cancelled":
                # `error` carries why; the note says what it means for her.
                payload["note"] = (
                    "not sent: nobody saw this. Say it again if it still matters."
                )
            if error:
                payload["error"] = error[:300]
            # Synthetic IDs must avoid collisions even if old events were deleted.
            update_id = conn.execute(
                "SELECT MIN(COALESCE(MIN(update_id), 0), 0) - 1 FROM observation_events"
            ).fetchone()[0]
            cursor = conn.execute(
                "INSERT INTO observation_events (update_id, chat_id, topic_id, kind, received_at, payload_json)"
                " VALUES (?, ?, ?, 'action_result', ?, ?)",
                (update_id, chat_id, topic_id, self._now(), json.dumps(payload, ensure_ascii=False)),
            )
            sequence = int(cursor.lastrowid)
        conn.execute("INSERT INTO action_result_events (action_id, sequence) VALUES (?, ?)",
                     (action_id, sequence))
        return sequence

    def append_synthetic_observation(
        self, *, dedupe_key: str, chat_id: int, topic_id: int | None,
        kind: str, payload: dict[str, Any],
    ) -> int | None:
        """Put an application-generated event into the observation stream.

        Two things depend on this:

        * A timer the agent registered (`wait_until`) must actually produce an
          activation. Before this, a due timer only re-armed the batch window,
          and `_batches` discarded it for having no pending observations - so
          the agent's own "look at this again in two minutes" never fired
          unless somebody unrelated happened to speak.
        * The agent must be able to see *why* it is being woken. A bare
          activation with no new messages would be indistinguishable from a
          random interruption; the payload carries the purpose it wrote down.

        Idempotent on `dedupe_key`: a tick that runs twice, or a process that
        restarts between the insert and the wake, cannot inject a duplicate.
        Returns the sequence, or None when this key was already present.
        """
        with self._write(immediate=True) as conn:
            if conn.execute(
                "SELECT 1 FROM synthetic_observations WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone():
                return None
            # Synthetic ids must stay clear of real (always positive) update
            # ids and of the ones action receipts use.
            update_id = conn.execute(
                "SELECT MIN(COALESCE(MIN(update_id), 0), 0) - 1 FROM observation_events"
            ).fetchone()[0]
            cursor = conn.execute(
                "INSERT INTO observation_events"
                " (update_id, chat_id, topic_id, kind, received_at, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (update_id, chat_id, topic_id, kind, self._now(),
                 json.dumps(payload, ensure_ascii=False)),
            )
            sequence = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO synthetic_observations (dedupe_key, sequence)"
                " VALUES (?, ?)", (dedupe_key, sequence),
            )
            return sequence

    def record_observation_skip(
        self, *, sequence: int, chat_id: int, topic_id: int | None, reason: str,
    ) -> None:
        """Record an observation that will never be shown. Never silent."""
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO observation_skips"
                " (sequence, chat_id, topic_id, reason, recorded_at) VALUES (?, ?, ?, ?, ?)",
                (sequence, chat_id, topic_id, reason[:200], self._now()),
            )

    # -- agent waits and state ---------------------------------------------

    def add_wait(
        self, *, wait_id: str, chat_id: int, topic_id: int | None, kind: str,
        purpose: str, event_types: list[str] | None = None,
        due_at: float | None = None, expires_at: float | None = None,
        episode_id: str | None = None,
    ) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_waits (wait_id, chat_id, topic_id, kind,"
                " purpose, event_types_json, due_at, expires_at, status, created_at,"
                " resolved_at, episode_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?)",
                (wait_id, chat_id, topic_id, kind, purpose,
                 json.dumps(event_types, ensure_ascii=False) if event_types else None,
                 due_at, expires_at, self._now(), episode_id),
            )

    # -- agent intents (continuation across activations) ---------------------

    def open_intent(
        self, *, intent_id: str, chat_id: int, topic_id: int | None,
        goal: str, expires_at: float | None = None,
    ) -> bool:
        """Record what the agent is now in the middle of.

        Returns False when this scope already has an open intent, which the
        partial unique index enforces at the storage layer: two concurrent
        activations for one topic must not both invent a goal.
        """
        if not goal.strip():
            return False
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT INTO agent_intents (intent_id, chat_id, topic_id, goal,"
                    " status, created_at, updated_at, expires_at)"
                    " VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
                    (intent_id, chat_id, topic_id, goal[:1000], self._now(),
                     self._now(), expires_at),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def current_intent(self, chat_id: int, topic_id: int | None) -> sqlite3.Row | None:
        """The scope's one open intent, if any."""
        rows = self._query(
            "SELECT * FROM agent_intents WHERE chat_id = ? AND topic_id IS ?"
            " AND status = 'open' ORDER BY created_at LIMIT 1",
            (chat_id, topic_id),
        )
        return rows[0] if rows else None

    def replace_intent(self, *, intent_id: str, chat_id: int, topic_id: int | None,
                       goal: str, expires_at: float | None) -> None:
        """Replace the goal with a new identity so old sends cannot close it."""
        with self._write(immediate=True) as conn:
            now = self._now()
            conn.execute(
                "UPDATE agent_intents SET status = 'abandoned', resolved_by = 'replaced',"
                " resolved_at = ?, updated_at = ? WHERE chat_id = ? AND topic_id IS ?"
                " AND status = 'open'", (now, now, chat_id, topic_id))
            conn.execute(
                "INSERT INTO agent_intents (intent_id, chat_id, topic_id, goal, status,"
                " created_at, updated_at, expires_at) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
                (intent_id, chat_id, topic_id, goal, now, now, expires_at))

    def update_intent_goal(self, intent_id: str, goal: str) -> bool:
        """Refine the goal without closing it: what she wants can change."""
        if not goal.strip():
            return False
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE agent_intents SET goal = ?, updated_at = ?"
                " WHERE intent_id = ? AND status = 'open'",
                (goal[:1000], self._now(), intent_id),
            )
            return cursor.rowcount == 1

    def intent_is_open(self, intent_id: str) -> bool:
        """Whether a specific goal is still open. Used after delivery."""
        rows = self._query(
            "SELECT 1 FROM agent_intents WHERE intent_id = ? AND status = 'open'"
            " LIMIT 1", (intent_id,))
        return bool(rows)

    def resolve_intent(
        self, intent_id: str, *, how: str, status: str = "resolved",
    ) -> bool:
        """Close an intent. Idempotent: closing twice reports False, not an error."""
        if status not in {"resolved", "abandoned"}:
            raise ValueError(f"not a terminal intent status: {status}")
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE agent_intents SET status = ?, resolved_by = ?,"
                " resolved_at = ?, updated_at = ?"
                " WHERE intent_id = ? AND status = 'open'",
                (status, how[:200], self._now(), self._now(), intent_id),
            )
            return cursor.rowcount == 1

    def expire_intents(self, now: float | None = None) -> list[sqlite3.Row]:
        """Close intents whose deadline passed. Returns what was expired.

        Expiry is a resolution, not a failure: it is reported back to the agent
        so a forgotten goal does not silently keep colouring its context.
        """
        moment = self._now() if now is None else now
        with self._write(immediate=True) as conn:
            rows = list(conn.execute(
                "SELECT * FROM agent_intents WHERE status = 'open'"
                " AND expires_at IS NOT NULL AND expires_at <= ?", (moment,)))
            for row in rows:
                conn.execute(
                    "UPDATE agent_intents SET status = 'abandoned', resolved_by = 'expired',"
                    " resolved_at = ?, updated_at = ? WHERE intent_id = ? AND status = 'open'",
                    (moment, moment, row["intent_id"]))
                update_id = conn.execute(
                    "SELECT MIN(COALESCE(MIN(update_id), 0), 0) - 1 FROM observation_events"
                ).fetchone()[0]
                payload = {"intent_id": row["intent_id"], "goal": row["goal"],
                           "note": "This goal reached its deadline and is closed."}
                cursor = conn.execute(
                    "INSERT INTO observation_events (update_id, chat_id, topic_id, kind, received_at, payload_json)"
                    " VALUES (?, ?, ?, 'intent_expired', ?, ?)",
                    (update_id, row["chat_id"], row["topic_id"], moment,
                     json.dumps(payload, ensure_ascii=False)))
                conn.execute("INSERT INTO synthetic_observations (dedupe_key, sequence) VALUES (?, ?)",
                             (f"intent-expired:{row['intent_id']}", cursor.lastrowid))
            return rows

    def active_waits(self, chat_id: int, topic_id: int | None) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM agent_waits WHERE chat_id = ? AND topic_id IS ? AND status = 'active'"
            " ORDER BY created_at", (chat_id, topic_id),
        )

    def due_waits(self, now: float) -> list[sqlite3.Row]:
        """Timers that have come due. Firing only grants another look."""
        return self._query(
            "SELECT * FROM agent_waits WHERE status = 'active'"
            " AND ((due_at IS NOT NULL AND due_at <= ?)"
            "  OR (expires_at IS NOT NULL AND expires_at <= ?))"
            " ORDER BY due_at", (now, now),
        )

    def resolve_wait(self, wait_id: str, status: str) -> bool:
        if status not in {"cancelled", "expired", "satisfied"}:
            raise ValueError(f"bad wait status: {status}")
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE agent_waits SET status = ?, resolved_at = ?"
                " WHERE wait_id = ? AND status = 'active'", (status, self._now(), wait_id),
            )
            return cursor.rowcount == 1

    def satisfy_event_waits(
        self, chat_id: int, topic_id: int | None, kind: str, now: float,
    ) -> list[str]:
        """Resolve waits whose expiry passed and report ones this event matches.

        Matching is structural only (topic + allowed kinds). Anything semantic
        is left to the agent when it is woken again.
        """
        satisfied: list[str] = []
        for row in self.active_waits(chat_id, topic_id):
            expires_at = row["expires_at"]
            if expires_at is not None and float(expires_at) <= now:
                self.resolve_wait(str(row["wait_id"]), "expired")
                continue
            allowed = row["event_types_json"]
            kinds = json.loads(allowed) if allowed else None
            if kinds is not None and kind in kinds:
                if self.resolve_wait(str(row["wait_id"]), "satisfied"):
                    satisfied.append(str(row["wait_id"]))
            elif kinds is None and kind != "action_result":
                # An unfiltered wait means "something new from the group". The
                # agent's own delivery receipt is not that: letting it satisfy
                # the wait would mean a message she sent wakes the very wait
                # that was meant to await someone else's answer. Asking for
                # `action_result` explicitly still works.
                if self.resolve_wait(str(row["wait_id"]), "satisfied"):
                    satisfied.append(str(row["wait_id"]))
        return satisfied

    def set_state(
        self, *, chat_id: int, topic_id: int | None, key: str, value: Any,
        source: str = "agent", expires_at: float | None = None,
    ) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO agent_state (chat_id, topic_id, key, value_json, source,"
                " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(chat_id, topic_id, key) DO UPDATE SET"
                " value_json = excluded.value_json, source = excluded.source,"
                " created_at = excluded.created_at, expires_at = excluded.expires_at",
                (chat_id, topic_id, key,
                 json.dumps(value, ensure_ascii=False, allow_nan=False),
                 source, self._now(), expires_at),
            )

    def get_state(self, chat_id: int, topic_id: int | None, *, limit: int = 50) -> dict[str, Any]:
        rows = self._query(
            "SELECT key, value_json, expires_at FROM agent_state"
            " WHERE chat_id = ? AND topic_id IS ? ORDER BY created_at DESC LIMIT ?",
            (chat_id, topic_id, limit),
        )
        now = self._now()
        return {
            str(row["key"]): json.loads(row["value_json"])
            for row in rows
            if row["expires_at"] is None or float(row["expires_at"]) > now
        }

    # -- agent actions (intent before effect) -------------------------------

    def prepare_action(
        self, *, action_id: str, workflow_id: str | None, chat_id: int,
        topic_id: int | None, text: str, observed_through: int,
        reply_to: int | None = None, freshness: str = "strict",
        max_unobserved: int = 0, expires_at: float | None = None,
        intent_id: str | None = None,
    ) -> bool:
        """Record the intent once. Re-preparing the same ID is a no-op."""
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO actions (action_id, workflow_id, chat_id, topic_id,"
                " reply_to, text, observed_through, freshness, max_unobserved, status,"
                " created_at, updated_at, expires_at, intent_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (action_id, workflow_id, chat_id, topic_id, reply_to, text,
                 observed_through, freshness, max_unobserved, self._now(), self._now(),
                 expires_at, intent_id),
            )
            return cursor.rowcount == 1

    def claim_action(
        self, action_id: str, *, now: float | None = None,
        max_actions_per_hour: int | None = None,
        min_action_interval_seconds: float = 0,
    ) -> str:
        """Atomically check freshness and chat delivery limits, then claim.

        Returns "claimed", "stale", "expired", "throttled", or
        "not-claimable". A throttled action remains pending. Delivery limits
        count sent, unknown and in-flight actions by their updated time.
        Checks and the status transition share one write transaction.
        """
        moment = self._now() if now is None else now
        with self._write(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return "not-claimable"
            if row["workflow_id"] is not None:
                workflow = conn.execute("SELECT state FROM workflows WHERE workflow_id = ?",
                                        (row["workflow_id"],)).fetchone()
                if workflow is None or workflow["state"] != "completed":
                    return "not-claimable"
            if row["expires_at"] is not None and float(row["expires_at"]) <= moment:
                conn.execute(
                    "UPDATE actions SET status = 'cancelled',"
                    " error = 'expired_unexecuted', updated_at = ? WHERE action_id = ?",
                    (moment, action_id),
                )
                return "expired"
            # Sequence numbers are global, so gaps can belong to other scopes.
            # Count actual events after the watermark while holding the write lock.
            unobserved = conn.execute(
                "SELECT COUNT(*) FROM observation_events"
                " WHERE chat_id = ? AND topic_id IS ? AND sequence > ?",
                (row["chat_id"], row["topic_id"], row["observed_through"]),
            ).fetchone()[0]
            if row["freshness"] == "strict" and unobserved:
                return "stale"
            if row["freshness"] == "bounded" and unobserved > int(row["max_unobserved"]):
                return "stale"
            if min_action_interval_seconds > 0 or max_actions_per_hour is not None:
                usage = conn.execute(
                    "SELECT MAX(updated_at) AS last,"
                    " SUM(CASE WHEN updated_at >= ? THEN 1 ELSE 0 END) AS used"
                    " FROM actions WHERE chat_id = ? AND status IN ('sent', 'unknown', 'sending')",
                    (moment - 3600, row["chat_id"]),
                ).fetchone()
                if (min_action_interval_seconds > 0 and usage["last"] is not None
                        and moment - float(usage["last"]) < min_action_interval_seconds):
                    return "throttled"
                if max_actions_per_hour is not None and int(usage["used"] or 0) >= max_actions_per_hour:
                    return "throttled"
            changed = conn.execute(
                "UPDATE actions SET status = 'sending', attempts = attempts + 1,"
                " updated_at = ? WHERE action_id = ? AND status = 'pending'",
                (moment, action_id),
            )
            return "claimed" if changed.rowcount == 1 else "not-claimable"

    def finish_action(self, action_id: str, status: str, *,
                      telegram_message_id: int | None = None, error: str | None = None,
                      sender_label: str = "Alma", sender_id: int | None = None) -> bool:
        """Commit a terminal status and its durable consequences together."""
        if status not in {"sent", "failed", "unknown", "cancelled"}:
            raise ValueError(f"bad action status: {status}")
        with self._write(immediate=True) as conn:
            row = conn.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).fetchone()
            if row is None or row["status"] not in ({"pending", "sending"} if status == "cancelled" else {"sending"}):
                return False
            conn.execute(
                "UPDATE actions SET status = ?, telegram_message_id = ?, error = ?, updated_at = ? WHERE action_id = ?",
                (status, telegram_message_id, (error or "")[:500] or None, self._now(), action_id),
            )
            if status == "sent" and row["intent_id"]:
                conn.execute(
                    "UPDATE agent_intents SET status = 'resolved', resolved_by = 'send_message',"
                    " resolved_at = ?, updated_at = ? WHERE intent_id = ? AND status = 'open'",
                    (self._now(), self._now(), row["intent_id"]))
            if status in {"failed", "unknown", "cancelled"}:
                # Same transaction as the status change: a crash between them
                # would leave a message the agent believes was sent, or one
                # silently dropped with no record that it ever existed.
                self._record_action_result(conn, action_id=action_id, chat_id=row["chat_id"],
                                           topic_id=row["topic_id"], status=status, text=row["text"], error=error)
            elif status == "sent" and telegram_message_id is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO messages (chat_id, message_id, topic_id, sender_id, sender_label,"
                    " is_bot, text, created_at, update_id, reply_to_bot) VALUES (?, ?, ?, ?, ?, 1, ?, ?, -1, 0)",
                    (row["chat_id"], telegram_message_id, row["topic_id"], sender_id, sender_label, row["text"], self._now()),
                )
            return True

    def recover_orphaned_actions(self) -> list[str]:
        """Atomically report interrupted sends and repair older missing receipts."""
        with self._write(immediate=True) as conn:
            rows = list(conn.execute("SELECT * FROM actions WHERE status = 'sending'"))
            for row in rows:
                error = "process exited during send; delivery unconfirmed"
                conn.execute("UPDATE actions SET status = 'unknown', error = ?, updated_at = ? WHERE action_id = ? AND status = 'sending'",
                             (error, self._now(), row["action_id"]))
            # Older versions could commit a terminal state without its event.
            # `cancelled` is included: a reply dropped because the conversation
            # moved on is something the agent must learn about, and before this
            # the decision was invisible to her.
            for row in conn.execute(
                "SELECT * FROM actions WHERE status IN ('failed', 'unknown', 'cancelled')"
            ):
                self._record_action_result(conn, action_id=row["action_id"], chat_id=row["chat_id"],
                                           topic_id=row["topic_id"], status=row["status"], text=row["text"], error=row["error"])
            return [str(row["action_id"]) for row in rows]

    def pending_actions(self, *, limit: int = 50) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM actions WHERE status = 'pending' ORDER BY created_at LIMIT ?",
            (limit,),
        )

    def get_action(self, action_id: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM actions WHERE action_id = ?", (action_id,))
        return rows[0] if rows else None

    # -- cost and disturbance budgets --------------------------------------

    def count_actions_since(self, chat_id: int, since: float) -> int:
        """How many messages actually reached the group since `since`.

        Counts `sent` and `unknown`: an unknown delivery may well be visible,
        so it must consume budget rather than let a retry storm through.
        """
        rows = self._query(
            "SELECT COUNT(*) AS n FROM actions WHERE chat_id = ?"
            " AND status IN ('sent', 'unknown') AND created_at >= ?",
            (chat_id, since),
        )
        return int(rows[0]["n"]) if rows else 0

    def last_action_at(self, chat_id: int) -> float | None:
        rows = self._query(
            "SELECT MAX(created_at) AS t FROM actions WHERE chat_id = ?"
            " AND status IN ('sent', 'unknown')",
            (chat_id,),
        )
        return float(rows[0]["t"]) if rows and rows[0]["t"] is not None else None

    def count_activations_since(self, chat_id: int, since: float) -> int:
        """Model opportunities granted since `since`, across all topics.

        This is the cost brake: observations can arrive freely, but the number
        of times the model is asked to look is bounded per hour.
        """
        rows = self._query(
            "SELECT COUNT(*) AS n FROM workflows WHERE chat_id = ? AND created_at >= ?",
            (chat_id, since),
        )
        return int(rows[0]["n"]) if rows else 0

    def record_bot_message(self, *, chat_id: int, topic_id: int | None, message_id: int,
                           text: str, sender_label: str, sender_id: int | None,
                           created_at: float) -> None:
        """Record our own confirmed message so later turns can see it.

        Only called after Telegram confirmed delivery; an unknown result must
        never be written as public conversation history.
        """
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO messages (chat_id, message_id, topic_id, sender_id,"
                " sender_label, is_bot, text, created_at, update_id, reply_to_bot)"
                " VALUES (?, ?, ?, ?, ?, 1, ?, ?, -1, 0)",
                (chat_id, message_id, topic_id, sender_id, sender_label, text, created_at),
            )

    # -- inbound -----------------------------------------------------------

    def record_update(
        self,
        *,
        update_id: int,
        chat_id: int | None,
        message_id: int | None,
        payload: dict[str, Any],
    ) -> bool:
        """Persist an update. Returns False if it was already known.

        This is the dedupe gate: Telegram may redeliver any update, and a
        restart may replay the same batch. The caller must not act on a False.
        """
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO updates (update_id, chat_id, message_id, received_at, payload_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (update_id, chat_id, message_id, self._now(), json.dumps(payload, ensure_ascii=False)),
            )
            return cursor.rowcount == 1

    def record_reaction_feedback(
        self, *, update_id: int, chat_id: int, message_id: int,
        old_vote: str | None, new_vote: str | None,
    ) -> str:
        """Atomically persist a reaction update and its vote delta.

        Actor identity is deliberately discarded. The old/new state reported by
        Telegram is enough to maintain aggregate counts, including vote changes.
        """
        allowed = {None, "up", "down"}
        if old_vote not in allowed or new_vote not in allowed:
            return "invalid"
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO updates (update_id, chat_id, message_id, received_at, payload_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (update_id, chat_id, message_id, self._now(), json.dumps({
                    "kind": "message_reaction",
                    "old_vote": old_vote,
                    "new_vote": new_vote,
                })),
            )
            if cursor.rowcount != 1:
                return "duplicate"
            sent = conn.execute(
                "SELECT decision_id, topic_id FROM sends"
                " WHERE chat_id = ? AND telegram_message_id = ? AND status = 'sent'",
                (chat_id, message_id),
            ).fetchone()
            if sent is None:
                return "unknown_target"
            conn.execute(
                "INSERT INTO observation_events"
                " (update_id, chat_id, topic_id, kind, received_at, payload_json)"
                " VALUES (?, ?, ?, 'feedback', ?, ?)",
                (update_id, chat_id, sent["topic_id"], self._now(), json.dumps({
                    "message_id": message_id, "old_vote": old_vote, "new_vote": new_vote,
                })),
            )
            if old_vote == new_vote:
                return "no_change"
            conn.execute(
                "INSERT INTO feedback_events"
                " (update_id, chat_id, message_id, decision_id, old_vote, new_vote, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (update_id, chat_id, message_id, sent["decision_id"],
                 old_vote, new_vote, self._now()),
            )
            return "recorded"

    def feedback_report(self, chat_id: int | None = None) -> dict[str, Any]:
        """Read-only current vote totals and per-reply aggregates."""
        where = " WHERE s.chat_id = ?" if chat_id is not None else ""
        params = (chat_id,) if chat_id is not None else ()
        rows = self._query(
            "SELECT f.decision_id, s.chat_id, s.topic_id, s.telegram_message_id, s.text,"
            " SUM(CASE WHEN f.new_vote = 'up' THEN 1 ELSE 0 END)"
            " - SUM(CASE WHEN f.old_vote = 'up' THEN 1 ELSE 0 END) AS up,"
            " SUM(CASE WHEN f.new_vote = 'down' THEN 1 ELSE 0 END)"
            " - SUM(CASE WHEN f.old_vote = 'down' THEN 1 ELSE 0 END) AS down,"
            " MAX(f.created_at) AS latest_at"
            " FROM feedback_events f JOIN sends s ON s.decision_id = f.decision_id"
            + where
            + " GROUP BY f.decision_id, s.chat_id, s.topic_id, s.telegram_message_id, s.text"
            " ORDER BY latest_at DESC",
            params,
        )
        items = [{
            "decision_id": str(row["decision_id"]),
            "chat_id": int(row["chat_id"]),
            "topic_id": row["topic_id"],
            "message_id": row["telegram_message_id"],
            "text": str(row["text"]),
            "up": int(row["up"] or 0),
            "down": int(row["down"] or 0),
            "latest_at": float(row["latest_at"]),
        } for row in rows]
        return {
            "up": sum(item["up"] for item in items),
            "down": sum(item["down"] for item in items),
            "items": items,
        }

    def has_update(self, update_id: int) -> bool:
        return bool(self._query("SELECT 1 FROM updates WHERE update_id = ?", (update_id,)))

    def record_inbound_message(
        self, message: StoredMessage, *, update_id: int, payload: dict[str, Any]
    ) -> bool:
        """Persist the deduplication key and message in one transaction."""
        with self._write() as conn:
            update = conn.execute(
                "INSERT OR IGNORE INTO updates (update_id, chat_id, message_id, received_at, payload_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (update_id, message.chat_id, message.message_id, self._now(),
                 json.dumps(payload, ensure_ascii=False)),
            )
            if update.rowcount != 1:
                return False
            conn.execute(
                "INSERT INTO observation_events"
                " (update_id, chat_id, topic_id, kind, received_at, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (update_id, message.chat_id, message.topic_id,
                 "message_edit" if conn.execute(
                     "SELECT 1 FROM messages WHERE chat_id = ? AND message_id = ?",
                     (message.chat_id, message.message_id),
                 ).fetchone() else "message", self._now(), json.dumps({
                     "message_id": message.message_id, "sender_id": message.sender_id,
                     "sender_label": message.sender_label, "is_bot": message.is_bot,
                    "text": message.text, "created_at": message.created_at,
                    "reply_to_bot": message.reply_to_bot,
                    # Only present when there was an attachment, so a plain
                    # text message keeps its old payload shape.
                    **({"media": {"kind": message.media_kind,
                                  "hint": message.media_hint,
                                  **({"file_id": message.media_file_id} if message.media_file_id else {}),
                                  **({"preview": message.media_preview} if message.media_preview else {})}}
                       if message.media_kind else {}),
                }, ensure_ascii=False)),
            )
            inserted = conn.execute(
                "INSERT OR IGNORE INTO messages (chat_id, message_id, topic_id, sender_id,"
                " sender_label, is_bot, text, created_at, update_id, reply_to_bot)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (message.chat_id, message.message_id, message.topic_id, message.sender_id,
                 message.sender_label, int(message.is_bot), message.text, message.created_at,
                 update_id, int(message.reply_to_bot)),
            )
            return inserted.rowcount == 1

    def record_message(self, message: StoredMessage, *, update_id: int) -> bool:
        """Persist a chat message. Returns False if (chat_id, message_id) exists."""
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO messages (chat_id, message_id, topic_id, sender_id,"
                " sender_label, is_bot, text, created_at, update_id, reply_to_bot)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.chat_id,
                    message.message_id,
                    message.topic_id,
                    message.sender_id,
                    message.sender_label,
                    1 if message.is_bot else 0,
                    message.text,
                    message.created_at,
                    update_id,
                    1 if message.reply_to_bot else 0,
                ),
            )
            return cursor.rowcount == 1

    def recent_messages(
        self, chat_id: int, *, topic_id: int | None, limit: int = 30
    ) -> list[StoredMessage]:
        """Newest `limit` messages for one chat+topic, oldest first.

        Topic scoping is mandatory: a forum group with two topics must never
        see one topic's history while answering in the other.
        """
        if topic_id is None:
            rows = self._query(
                "SELECT * FROM messages WHERE chat_id = ? AND topic_id IS NULL"
                " ORDER BY message_id DESC LIMIT ?",
                (chat_id, limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM messages WHERE chat_id = ? AND topic_id = ?"
                " ORDER BY message_id DESC LIMIT ?",
                (chat_id, topic_id, limit),
            )
        return [_row_to_message(row) for row in reversed(rows)]

    def unanswered_messages(self, *, limit: int = 200) -> list[StoredMessage]:
        """Inbound recovery: messages ingested but never covered by a turn.

        The crash window is "message row committed, turn not yet processed". A
        message is covered when some turn in the same (chat, topic) scope already
        reaches it — `turns.last_message` is the high-water message id of the
        window that was processed.

        At most the newest message per scope is returned: a new turn would run
        from there, with everything older serving as context.
        """
        scopes = self._query(
            "SELECT chat_id, topic_id, MAX(message_id) AS high"
            " FROM messages GROUP BY chat_id, topic_id ORDER BY chat_id"
        )
        result: list[StoredMessage] = []
        for scope in scopes[:limit]:
            chat_id = int(scope["chat_id"])
            topic_id = scope["topic_id"]
            high = int(scope["high"])
            if topic_id is None:
                covered = self._query(
                    "SELECT MAX(last_message) AS m FROM turns"
                    " WHERE chat_id = ? AND topic_id IS NULL",
                    (chat_id,),
                )
            else:
                covered = self._query(
                    "SELECT MAX(last_message) AS m FROM turns"
                    " WHERE chat_id = ? AND topic_id = ?",
                    (chat_id, topic_id),
                )
            highest_covered = covered[0]["m"] if covered else None
            if highest_covered is not None and highest_covered >= high:
                continue
            rows = self._query(
                "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, high),
            )
            if rows:
                result.append(_row_to_message(rows[0]))
        return result

    def recover_unanswered_messages(self, *, limit: int = 200) -> list[StoredMessage]:
        """Return all inbound messages not covered by a durable decision."""
        rows = self._query(
            "SELECT m.* FROM messages m WHERE m.is_bot = 0 AND NOT EXISTS ("
            " SELECT 1 FROM turns t JOIN decisions d ON d.turn_id = t.turn_id"
            " WHERE t.chat_id = m.chat_id AND t.topic_id IS m.topic_id"
            " AND m.message_id BETWEEN t.first_message AND t.last_message)"
            " ORDER BY m.chat_id, m.topic_id, m.message_id LIMIT ?", (limit,))
        return [_row_to_message(row) for row in rows]

    def recover_open_turns(self) -> list[sqlite3.Row]:
        return self._query("SELECT t.* FROM turns t WHERE t.closed_at IS NULL"
                           " AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.turn_id = t.turn_id)"
                           " ORDER BY t.opened_at")

    def messages_for_turn(self, turn: sqlite3.Row) -> list[StoredMessage]:
        rows = self._query("SELECT * FROM messages WHERE chat_id = ? AND topic_id IS ?"
                           " AND message_id BETWEEN ? AND ? ORDER BY message_id",
                           (turn['chat_id'], turn['topic_id'], turn['first_message'], turn['last_message']))
        return [_row_to_message(row) for row in rows if not row['is_bot']]

    def unfinished_decisions(self) -> list[sqlite3.Row]:
        return self._query("SELECT d.*, t.first_message, t.last_message, t.opened_at FROM decisions d"
                           " JOIN turns t ON t.turn_id = d.turn_id"
                           " WHERE t.closed_at IS NULL AND d.outcome = 'reply'"
                           " AND d.superseded_by IS NULL"
                           " AND NOT EXISTS (SELECT 1 FROM sends s WHERE s.decision_id = d.decision_id)"
                           " ORDER BY d.created_at")

    def get_update_cursor(self) -> int | None:
        rows = self._query("SELECT value FROM meta WHERE key = 'poll_offset'")
        return int(rows[0]["value"]) if rows else None

    def set_update_cursor(self, offset: int) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('poll_offset', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(offset),),
            )

    # -- turns and decisions ----------------------------------------------

    def open_turn(
        self, *, turn_id: str, chat_id: int, topic_id: int | None,
        first_message: int, last_message: int, trigger: str,
    ) -> None:
        """Claim a window. Idempotent: re-opening an existing turn is a no-op.

        INSERT OR REPLACE here would reset `outcome` and re-stamp `opened_at` if
        the same turn were opened twice, hiding a completed turn's result.
        """
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO turns (turn_id, chat_id, topic_id, first_message,"
                " last_message, opened_at, closed_at, trigger, outcome)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL)",
                (turn_id, chat_id, topic_id, first_message, last_message, self._now(), trigger),
            )

    def close_turn(self, turn_id: str, outcome: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE turns SET closed_at = ?, outcome = ? WHERE turn_id = ?",
                (self._now(), outcome, turn_id),
            )

    def record_decision(
        self,
        *,
        decision_id: str,
        chat_id: int,
        topic_id: int | None,
        turn_id: str,
        outcome: str,
        reason: str,
        detail: dict[str, Any],
        trigger: str,
    ) -> None:
        """Record a participation decision. Silence is a row, not an absence.

        `outcome` is 'reply' or 'silent'. A silent row is the audit trail
        required by the brief: silence must be a recorded decision, never an
        empty message.
        """
        assert outcome in {"reply", "silent"}, outcome
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO decisions (decision_id, chat_id, topic_id, turn_id,"
                " outcome, reason, detail_json, trigger, created_at, superseded_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    decision_id, chat_id, topic_id, turn_id, outcome, reason,
                    json.dumps(detail, ensure_ascii=False), trigger, self._now(),
                ),
            )

    def update_decision_detail(self, decision_id: str, detail: dict[str, Any]) -> bool:
        """Replace a decision's detail with the post-activation version.

        The policy decision is written before the model is called, so its detail
        holds only the trigger. Token usage, latency and prompt size are known
        afterwards. Without this the *successful* replies carry no cost data —
        only the failed ones did, because those write a new row — which makes
        throughput and cache-hit accounting impossible from the store.
        """
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE decisions SET detail_json = ? WHERE decision_id = ?",
                (json.dumps(detail, ensure_ascii=False), decision_id),
            )
            return cursor.rowcount == 1

    def supersede(self, decision_id: str, superseded_by: str) -> bool:
        """Mark a decision dead. Returns True if it was still reply-pending.

        A superseded decision must never be sent, even if its model call was
        already in flight when the new message arrived.
        """
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE decisions SET superseded_by = ?"
                " WHERE decision_id = ? AND superseded_by IS NULL AND outcome = 'reply'"
                " AND NOT EXISTS (SELECT 1 FROM sends s WHERE s.decision_id = decisions.decision_id"
                " AND s.status = 'sending')",
                (superseded_by, decision_id),
            )
            return cursor.rowcount == 1

    def is_superseded(self, decision_id: str) -> bool:
        rows = self._query(
            "SELECT superseded_by FROM decisions WHERE decision_id = ?", (decision_id,)
        )
        return bool(rows) and rows[0]["superseded_by"] is not None

    def last_decision_at(self, chat_id: int, outcome: str = "reply") -> float | None:
        rows = self._query(
            "SELECT MAX(created_at) AS t FROM decisions WHERE chat_id = ? AND outcome = ?",
            (chat_id, outcome),
        )
        return rows[0]["t"] if rows and rows[0]["t"] is not None else None

    def recent_decisions(self, chat_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM decisions WHERE chat_id = ? ORDER BY created_at DESC LIMIT ?",
            (chat_id, limit),
        )

    # -- outbound ----------------------------------------------------------

    def prepare_send(
        self, *, decision_id: str, chat_id: int, topic_id: int | None,
        reply_to: int | None, text: str,
    ) -> None:
        """Record the intent once, preserving any existing delivery state."""
        now = self._now()
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sends (decision_id, chat_id, topic_id, reply_to,"
                " text, status, attempts, telegram_message_id, error, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, ?)",
                (decision_id, chat_id, topic_id, reply_to, text, now, now),
            )

    def mark_sending(self, decision_id: str) -> bool:
        """Move pending → sending. Returns False if the send is not claimable.

        Claiming is the last point before the network request. A row already in
        'sending' means a previous attempt may have reached Telegram: we do not
        claim it again.
        """
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE sends SET status = 'sending', attempts = attempts + 1, updated_at = ?"
                " WHERE decision_id = ? AND status = 'pending'"
                " AND (EXISTS (SELECT 1 FROM decisions d WHERE d.decision_id = sends.decision_id"
                " AND d.outcome = 'reply' AND d.superseded_by IS NULL AND d.created_at >= ?"
                " AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.turn_id = d.turn_id AND t.opened_at < ?))"
                " OR NOT EXISTS (SELECT 1 FROM decisions d WHERE d.decision_id = sends.decision_id))"
                " AND created_at >= ? AND NOT EXISTS (SELECT 1 FROM messages m"
                " WHERE m.chat_id = sends.chat_id AND m.topic_id IS sends.topic_id"
                " AND m.is_bot = 0 AND m.message_id > sends.reply_to)",
                (self._now(), decision_id, self._now() - 86400, self._now() - 86400, self._now() - 86400),
            )
            return cursor.rowcount == 1

    def pending_obsolete(self, decision_id: str, *, max_age: float = 86400) -> bool:
        rows = self._query("SELECT s.created_at, s.reply_to, s.chat_id, s.topic_id, d.superseded_by"
                           " FROM sends s LEFT JOIN decisions d ON d.decision_id = s.decision_id"
                           " WHERE s.decision_id = ?", (decision_id,))
        if not rows:
            return True
        row = rows[0]
        if row['superseded_by'] is not None or self._now() - row['created_at'] > max_age:
            return True
        if row['reply_to'] is None:
            return False
        return bool(self._query("SELECT 1 FROM messages WHERE chat_id = ? AND topic_id IS ?"
                                " AND is_bot = 0 AND message_id > ? LIMIT 1",
                                (row['chat_id'], row['topic_id'], row['reply_to'])))

    def cancel_pending(self, decision_id: str, reason: str) -> None:
        with self._write() as conn:
            cursor = conn.execute("UPDATE sends SET status = 'failed', error = ?, updated_at = ?"
                         " WHERE decision_id = ? AND status = 'pending'", (reason, self._now(), decision_id))
            if cursor.rowcount:
                conn.execute("UPDATE turns SET closed_at = ?, outcome = 'superseded' WHERE turn_id ="
                             " (SELECT turn_id FROM decisions WHERE decision_id = ?) AND closed_at IS NULL",
                             (self._now(), decision_id))

    def finish_sent(self, decision_id: str, telegram_message_id: int, message: StoredMessage | None) -> None:
        """Commit confirmed delivery, turn closure and chat history together."""
        with self._write() as conn:
            conn.execute("UPDATE sends SET status = 'sent', telegram_message_id = ?, error = NULL, updated_at = ?"
                         " WHERE decision_id = ? AND status = 'sending'", (telegram_message_id, self._now(), decision_id))
            conn.execute("UPDATE turns SET closed_at = ?, outcome = 'sent' WHERE turn_id ="
                         " (SELECT turn_id FROM decisions WHERE decision_id = ?)", (self._now(), decision_id))
            if message is not None:
                conn.execute("INSERT OR IGNORE INTO messages (chat_id, message_id, topic_id, sender_id,"
                             " sender_label, is_bot, text, created_at, update_id, reply_to_bot)"
                             " VALUES (?, ?, ?, ?, ?, 1, ?, ?, -1, 0)",
                             (message.chat_id, message.message_id, message.topic_id, message.sender_id,
                              message.sender_label, message.text, message.created_at))

    def mark_sent(self, decision_id: str, telegram_message_id: int) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE sends SET status = 'sent', telegram_message_id = ?, error = NULL,"
                " updated_at = ? WHERE decision_id = ?",
                (telegram_message_id, self._now(), decision_id),
            )

    def mark_send_unknown(self, decision_id: str, error: str) -> None:
        """Terminal. Never retried automatically: the message may be visible."""
        with self._write() as conn:
            conn.execute(
                "UPDATE sends SET status = 'unknown', error = ?, updated_at = ?"
                " WHERE decision_id = ?",
                (error[:500], self._now(), decision_id),
            )

    def mark_send_failed(self, decision_id: str, error: str) -> None:
        """Telegram definitively rejected the request. Retryable by an operator."""
        with self._write() as conn:
            conn.execute(
                "UPDATE sends SET status = 'failed', error = ?, updated_at = ?"
                " WHERE decision_id = ?",
                (error[:500], self._now(), decision_id),
            )

    def get_send(self, decision_id: str) -> SendRecord | None:
        rows = self._query("SELECT * FROM sends WHERE decision_id = ?", (decision_id,))
        return _row_to_send(rows[0]) if rows else None

    def sends_in_status(self, status: str) -> list[SendRecord]:
        rows = self._query(
            "SELECT * FROM sends WHERE status = ? ORDER BY created_at", (status,)
        )
        return [_row_to_send(row) for row in rows]

    def recover_orphaned_sends(self) -> list[str]:
        """Startup: a 'sending' row outlived its process.

        We cannot know whether Telegram received it. Mark UNKNOWN, do not retry.
        """
        with self._write() as conn:
            rows = list(
                conn.execute("SELECT decision_id FROM sends WHERE status = 'sending'")
            )
            conn.execute(
                "UPDATE sends SET status = 'unknown',"
                " error = 'process exited during send; delivery unconfirmed', updated_at = ?"
                " WHERE status = 'sending'",
                (self._now(),),
            )
            return [row["decision_id"] for row in rows]

    def recover_pending_sends(self) -> list[SendRecord]:
        """Startup: 'pending' rows were prepared but never claimed. Safe to send."""
        return self.sends_in_status("pending")

    # -- admin -------------------------------------------------------------

    def purge_chat(self, chat_id: int) -> None:
        """Delete all conversation data for one chat. Keeps no excerpts."""
        with self._write() as conn:
            conn.execute("DELETE FROM observation_events WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM observation_skips WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM observation_cursors WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM workflows WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM autonomous_calls WHERE activation_id IN"
                         " (SELECT activation_id FROM autonomous_responses WHERE chat_id = ?)",
                         (chat_id,))
            conn.execute("DELETE FROM autonomous_responses WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM agent_waits WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM agent_state WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM actions WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM feedback_events WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM turns WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM decisions WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM sends WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM updates WHERE chat_id = ?", (chat_id,))

    def stats(self, chat_id: int | None = None) -> dict[str, int]:
        where = "" if chat_id is None else " WHERE chat_id = ?"
        params: tuple[Any, ...] = () if chat_id is None else (chat_id,)
        result: dict[str, int] = {}
        for table in ("messages", "turns", "decisions", "sends"):
            rows = self._query(f"SELECT COUNT(*) AS n FROM {table}{where}", params)
            result[table] = int(rows[0]["n"])
        rows = self._query(
            "SELECT COUNT(*) AS n FROM decisions WHERE outcome = 'silent'"
            + (" AND chat_id = ?" if chat_id is not None else ""),
            params,
        )
        result["silent_decisions"] = int(rows[0]["n"])
        rows = self._query(
            "SELECT COUNT(*) AS n FROM decisions WHERE outcome = 'reply'"
            + (" AND chat_id = ?" if chat_id is not None else ""),
            params,
        )
        result["reply_decisions"] = int(rows[0]["n"])
        return result


def _row_to_message(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        chat_id=int(row["chat_id"]),
        message_id=int(row["message_id"]),
        topic_id=row["topic_id"],
        sender_id=row["sender_id"],
        sender_label=str(row["sender_label"]),
        is_bot=bool(row["is_bot"]),
        text=str(row["text"]),
        created_at=float(row["created_at"]),
        reply_to_bot=bool(row["reply_to_bot"]),
    )


def _row_to_send(row: sqlite3.Row) -> SendRecord:
    return SendRecord(
        decision_id=str(row["decision_id"]),
        chat_id=int(row["chat_id"]),
        topic_id=row["topic_id"],
        reply_to=row["reply_to"],
        text=str(row["text"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        telegram_message_id=row["telegram_message_id"],
        error=row["error"],
    )
