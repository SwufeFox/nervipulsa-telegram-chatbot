"""Readable operational logs: scoped labels, safe summaries, periodic status.

The bot serves several chats at once, and a bare `chat_id=-1001234567890` in a
log line tells you neither what kind of chat it is nor how much of it is
waiting. Two things were missing in practice:

* A backlog was invisible. 397 observations could sit unprocessed while every
  individual line still looked healthy, because nothing ever printed the size
  of the queue.
* A message preview was all-or-nothing. Either the log said nothing about the
  content, or it printed the whole thing.

So formatting lives here:

* `scope()` renders one stable scope label, `chat:-100…(group)/topic:7`.
* `summary()` renders a scrubbed preview. It is opt-in through
  `AppConfig.log_summary_chars`, because writing message text to a terminal is
  a privacy decision, not a sensible default.
* `status_snapshot()` and `status_lines()` produce the periodic block that
  answers "what is actually happening right now": pending backlog per scope,
  age of the oldest unprocessed message, in-flight turns, budget usage.
"""
from __future__ import annotations

import re
import time
from typing import Any, Mapping

# The same shapes the Telegram transport scrubs before a string reaches a log:
# a token appears in Bot API URLs and in a few PTB error messages, and a user
# can paste one into a message that we then summarise.
_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
_BARE_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_WHITESPACE = re.compile(r"\s+")


def redact(text: str) -> str:
    """Strip anything shaped like a bot token."""
    text = _TOKEN_IN_URL.sub("/bot<redacted>", text)
    return _BARE_TOKEN.sub("<redacted-token>", text)


def scope(chat_id: int, topic_id: int | None = None,
          kind: str | None = None) -> str:
    """One stable label for a (chat, topic) pair.

    `kind` should come from the ingest path (`private`, `group`,
    `supergroup`); when it is unknown the id's sign is used, which is a real
    Bot API convention: positive ids are users (private chats), negative ones
    are groups and channels.
    """
    label = kind or ("group" if chat_id < 0 else "dm")
    text = f"chat:{chat_id}({label})"
    if topic_id is not None:
        text += f"/topic:{topic_id}"
    return text


def summary(text: Any, limit: int) -> str:
    """A preview of message text, or "" when previews are disabled.

    Never returns more than `limit` characters of content, collapses newlines
    so one message cannot break the one-line-per-event log format, and scrubs
    token shapes first.
    """
    if not limit or limit <= 0:
        return ""
    flat = _WHITESPACE.sub(" ", redact(str(text or ""))).strip()
    if not flat:
        return ""
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return flat


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def status_snapshot(store: Any, *, now: float | None = None,
                    window: float = 3600.0) -> dict[str, Any]:
    """What the scheduler is holding right now, per scope and in total.

    Reads only: no cursor is touched and no turn is started. A failure here
    must never take down the caller, so every query is defensive.
    """
    moment = time.time() if now is None else now
    since = moment - window

    # Oldest unprocessed message per scope. `received_at` is when *we* ingested
    # it; the message's own `created_at` is what a reader means by "how far
    # behind are we", so prefer it and fall back to ingest time.
    backlog: dict[tuple[int, int | None], dict[str, Any]] = {}
    for row in store._query(
        "SELECT o.chat_id AS chat_id, o.topic_id AS topic_id,"
        " COUNT(*) AS pending,"
        " MIN(COALESCE(json_extract(o.payload_json, '$.created_at'), o.received_at))"
        "   AS oldest"
        " FROM observation_events o"
        " LEFT JOIN observation_cursors c"
        "   ON c.chat_id = o.chat_id AND c.topic_id IS o.topic_id"
        " WHERE o.sequence > COALESCE(c.last_processed, 0)"
        " GROUP BY o.chat_id, o.topic_id"
    ):
        backlog[(int(row["chat_id"]), row["topic_id"])] = {
            "pending": int(row["pending"]),
            "oldest_age": max(0.0, moment - float(row["oldest"]))
            if row["oldest"] is not None else None,
        }

    running: dict[tuple[int, int | None], dict[str, Any]] = {}
    for row in store._query(
        "SELECT chat_id, topic_id, workflow_id, first_sequence, last_sequence,"
        " created_at FROM workflows WHERE state = 'running'"
    ):
        running[(int(row["chat_id"]), row["topic_id"])] = {
            "workflow_id": str(row["workflow_id"]),
            "sequences": (int(row["first_sequence"]), int(row["last_sequence"])),
            "age": max(0.0, moment - float(row["created_at"])),
        }

    activations: dict[int, int] = {}
    for row in store._query(
        "SELECT chat_id, COUNT(*) AS n FROM workflows WHERE created_at >= ?"
        " GROUP BY chat_id", (since,)
    ):
        activations[int(row["chat_id"])] = int(row["n"])

    # Delivery budget is spent by outcome time, matching `claim_action`.
    delivered: dict[int, int] = {}
    for row in store._query(
        "SELECT chat_id, COUNT(*) AS n FROM actions"
        " WHERE status IN ('sent', 'unknown') AND updated_at >= ? GROUP BY chat_id",
        (since,),
    ):
        delivered[int(row["chat_id"])] = int(row["n"])

    pending_actions: dict[int, dict[str, Any]] = {}
    for row in store._query(
        "SELECT chat_id, COUNT(*) AS n, MIN(created_at) AS oldest FROM actions"
        " WHERE status = 'pending' GROUP BY chat_id"
    ):
        pending_actions[int(row["chat_id"])] = {
            "count": int(row["n"]),
            "oldest_age": max(0.0, moment - float(row["oldest"]))
            if row["oldest"] is not None else None,
        }

    # Every scope we have ever processed belongs in the report, including the
    # fully caught-up ones: "this chat is quiet" is the answer to "why is the
    # bot not saying anything", and a report that only lists backlogs cannot
    # give it.
    known = {
        (int(row["chat_id"]), row["topic_id"])
        for row in store._query("SELECT chat_id, topic_id FROM observation_cursors")
    }
    known |= set(backlog) | set(running) | {
        (chat_id, None) for chat_id in pending_actions
    } | {
        (chat_id, None) for chat_id in delivered
    }

    keys = known
    scopes = []
    for key in sorted(keys, key=lambda item: (item[0], item[1] or 0)):
        chat_id, topic_id = key
        entry = {
            "chat_id": chat_id,
            "topic_id": topic_id,
            "pending": backlog.get(key, {}).get("pending", 0),
            "oldest_age": backlog.get(key, {}).get("oldest_age"),
            "activations": activations.get(chat_id, 0),
            "delivered": delivered.get(chat_id, 0),
            "pending_actions": pending_actions.get(chat_id, {}).get("count", 0),
            "oldest_action_age": pending_actions.get(chat_id, {}).get("oldest_age"),
            "running": running.get(key),
        }
        scopes.append(entry)

    return {
        "at": moment,
        "scopes": scopes,
        "totals": {
            "pending": sum(s["pending"] for s in scopes),
            "running": sum(1 for s in scopes if s["running"]),
            "activations": sum(activations.values()),
            "delivered": sum(delivered.values()),
            "pending_actions": sum(s["pending_actions"] for s in scopes),
        },
    }


def status_lines(snapshot: Mapping[str, Any], *,
                 kinds: Mapping[int, str] | None = None,
                 limits: Mapping[str, Any] | None = None) -> list[str]:
    """Render a snapshot as one header plus one line per scope."""
    kinds = kinds or {}
    limits = limits or {}
    totals = snapshot["totals"]
    header = (
        f"status: pending={totals['pending']} running={totals['running']} "
        f"pending_actions={totals['pending_actions']} "
        f"activations={totals['activations']}/{limits.get('max_activations_per_hour', '?')} "
        f"delivered={totals['delivered']}/{limits.get('max_actions_per_hour', '?')}"
    )
    lines = [header]
    for entry in snapshot["scopes"]:
        label = scope(entry["chat_id"], entry["topic_id"],
                      kinds.get(entry["chat_id"]))
        parts = [f"  {label}"]
        if entry["pending"]:
            parts.append(f"pending={entry['pending']}"
                         f"(oldest {_age(entry['oldest_age'])})")
        else:
            parts.append("pending=0")
        if entry["pending_actions"]:
            parts.append(f"unsent={entry['pending_actions']}"
                         f"(oldest {_age(entry['oldest_action_age'])})")
        parts.append(f"activations={entry['activations']}")
        parts.append(f"delivered={entry['delivered']}")
        running = entry["running"]
        if running:
            first, last = running["sequences"]
            parts.append(f"running={running['workflow_id']}"
                         f" seq={first}-{last} age={_age(running['age'])}")
        lines.append(" ".join(parts))
    return lines
