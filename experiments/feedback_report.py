"""Read a live bot database's anonymized thumbs-up/down feedback.

Usage:
    python experiments/feedback_report.py --db path/to/bot.sqlite3
    python experiments/feedback_report.py --db path/to/bot.sqlite3 --chat -1001234567890

The database is opened in SQLite read-only mode. Reactions are aggregated per
bot reply; no voter identities are present in this report.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def load_report(db_path: Path, chat_id: int | None = None) -> dict:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        where = " WHERE s.chat_id = ?" if chat_id is not None else ""
        params = (chat_id,) if chat_id is not None else ()
        rows = connection.execute(
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
        ).fetchall()
        items = [{
            "decision_id": row["decision_id"],
            "chat_id": row["chat_id"],
            "topic_id": row["topic_id"],
            "message_id": row["telegram_message_id"],
            "text": row["text"],
            "up": int(row["up"] or 0),
            "down": int(row["down"] or 0),
            "latest_at": row["latest_at"],
        } for row in rows]
        return {
            "up": sum(item["up"] for item in items),
            "down": sum(item["down"] for item in items),
            "items": items,
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--chat", type=int)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error(f"database does not exist: {args.db}")
    print(json.dumps(load_report(args.db, args.chat), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
