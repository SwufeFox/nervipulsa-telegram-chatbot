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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_report(db_path: Path, chat_id: int | None = None) -> dict:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        from app.feedback import feedback_report

        return feedback_report(connection, chat_id)
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
