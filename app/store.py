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
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 1

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

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params))

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
                (update_id, chat_id, message_id, _now(), json.dumps(payload, ensure_ascii=False)),
            )
            return cursor.rowcount == 1

    def has_update(self, update_id: int) -> bool:
        return bool(self._query("SELECT 1 FROM updates WHERE update_id = ?", (update_id,)))

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
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO turns (turn_id, chat_id, topic_id, first_message,"
                " last_message, opened_at, closed_at, trigger, outcome)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL)",
                (turn_id, chat_id, topic_id, first_message, last_message, _now(), trigger),
            )

    def close_turn(self, turn_id: str, outcome: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE turns SET closed_at = ?, outcome = ? WHERE turn_id = ?",
                (_now(), outcome, turn_id),
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
                    json.dumps(detail, ensure_ascii=False), trigger, _now(),
                ),
            )

    def supersede(self, decision_id: str, superseded_by: str) -> bool:
        """Mark a decision dead. Returns True if it was still reply-pending.

        A superseded decision must never be sent, even if its model call was
        already in flight when the new message arrived.
        """
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE decisions SET superseded_by = ?"
                " WHERE decision_id = ? AND superseded_by IS NULL AND outcome = 'reply'",
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
        """Record the intent to send, before any Telegram request is made."""
        now = _now()
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sends (decision_id, chat_id, topic_id, reply_to,"
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
                " WHERE decision_id = ? AND status = 'pending'",
                (_now(), decision_id),
            )
            return cursor.rowcount == 1

    def mark_sent(self, decision_id: str, telegram_message_id: int) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE sends SET status = 'sent', telegram_message_id = ?, error = NULL,"
                " updated_at = ? WHERE decision_id = ?",
                (telegram_message_id, _now(), decision_id),
            )

    def mark_send_unknown(self, decision_id: str, error: str) -> None:
        """Terminal. Never retried automatically: the message may be visible."""
        with self._write() as conn:
            conn.execute(
                "UPDATE sends SET status = 'unknown', error = ?, updated_at = ?"
                " WHERE decision_id = ?",
                (error[:500], _now(), decision_id),
            )

    def mark_send_failed(self, decision_id: str, error: str) -> None:
        """Telegram definitively rejected the request. Retryable by an operator."""
        with self._write() as conn:
            conn.execute(
                "UPDATE sends SET status = 'failed', error = ?, updated_at = ?"
                " WHERE decision_id = ?",
                (error[:500], _now(), decision_id),
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
                (_now(),),
            )
            return [row["decision_id"] for row in rows]

    def recover_pending_sends(self) -> list[SendRecord]:
        """Startup: 'pending' rows were prepared but never claimed. Safe to send."""
        return self.sends_in_status("pending")

    # -- admin -------------------------------------------------------------

    def purge_chat(self, chat_id: int) -> None:
        """Delete all conversation data for one chat. Keeps no excerpts."""
        with self._write() as conn:
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
