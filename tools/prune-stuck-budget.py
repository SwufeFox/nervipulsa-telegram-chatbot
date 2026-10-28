"""Disabled: the old pruning algorithm could delete valid workflow records.

Grouping only by first_sequence cannot distinguish legitimate attempts, and
copying a SQLite database file is not a reliable backup when WAL is in use.
The original command is deliberately disabled for both dry-run and --apply so
old instructions cannot accidentally cause additional data loss. Preserve any
existing database and backup for inspection; do not attempt automatic repair.
"""
from __future__ import annotations

import sys


def main() -> int:
    print(
        "This tool has been disabled: it could delete valid workflow records "
        "and its backup may be incomplete. No changes were made.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
