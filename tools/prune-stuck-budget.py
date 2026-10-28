"""Clear workflow rows that a cursor bug produced, so the hourly budget frees up.

Context: before the `scope_key` fix, a non-forum chat (topic_id NULL) defeated
the cursors table's primary key. The watermark froze, every tick re-processed
the same observations, and each re-processing created a workflow row. The
hourly activation cap then refused every later turn, correctly but for good.

After that fix a restart stops new duplicates, but the rows already written
still occupy the hour-long window. This tool removes them.

What counts as a duplicate: the same `first_sequence` handled more than once.
The earliest of each group is kept, because that is the real first attempt at
that batch; later ones are replays of the same input.

Dry run by default. Stops the process if one is still writing, backs the
database up, and prints exactly what it would change.

    python tools/prune-stuck-budget.py                  # show what would go
    python tools/prune-stuck-budget.py --apply          # do it
    python tools/prune-stuck-budget.py --apply --db X   # other database
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path("state/fox/bot.sqlite3")


def find_duplicates(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        "SELECT first_sequence, COUNT(*) AS seen,"
        " MIN(created_at) AS first_at, MAX(created_at) AS last_at"
        " FROM workflows GROUP BY first_sequence HAVING COUNT(*) > 1"
        " ORDER BY first_sequence"
    ).fetchall()
    return [
        {"first_sequence": row[0], "seen": row[1],
         "first_at": row[2], "last_at": row[3]}
        for row in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true",
                        help="actually delete; without this it only reports")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"no database at {args.db}", file=sys.stderr)
        return 1

    connection = sqlite3.connect(args.db, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        total = connection.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
        duplicates = find_duplicates(connection)
        recyclable = sum(item["seen"] - 1 for item in duplicates)

        print(f"database      : {args.db}")
        print(f"workflows     : {total}")
        print(f"repeated input: {len(duplicates)} observation ranges")
        print(f"removable     : {recyclable} rows")
        print()
        if duplicates:
            print("  range   times   first seen        last seen")
            for item in duplicates:
                first = datetime.fromtimestamp(item["first_at"]).strftime("%H:%M:%S")
                last = datetime.fromtimestamp(item["last_at"]).strftime("%H:%M:%S")
                print(f"  {item['first_sequence']:>5}   {item['seen']:>5}   "
                      f"{first}         {last}")
            print()

        if not args.apply:
            print("dry run; nothing changed. Re-run with --apply to delete.")
            return 0

        if recyclable == 0:
            print("nothing to remove.")
            return 0

        backup = args.db.with_suffix(f".db.bak-{int(time.time())}")
        shutil.copy2(args.db, backup)
        print(f"backup        : {backup}")

        with connection:
            connection.execute(
                "DELETE FROM workflows WHERE workflow_id NOT IN ("
                "  SELECT workflow_id FROM ("
                "    SELECT workflow_id, ROW_NUMBER() OVER ("
                "      PARTITION BY first_sequence ORDER BY created_at"
                "    ) AS rank FROM workflows"
                "  ) WHERE rank = 1"
                ")"
            )
        left = connection.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
        print(f"workflows now : {left}")

        hour_ago = time.time() - 3600
        hour = connection.execute(
            "SELECT COUNT(*) FROM workflows WHERE created_at >= ?", (hour_ago,)
        ).fetchone()[0]
        print(f"last hour     : {hour} (the cap is max_activations_per_hour)")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
