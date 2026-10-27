"""Report cache usage for identical requests separated by a fixed sleep.

The sleep is after each response, so request durations also affect pacing.
The content-only prefix probe does not establish HTTP byte identity, and a
burst of hits cannot exclude multiple replicas. A reported zero on an unchanged
payload does not establish eviction or identify its cause.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from experiments.cache_ttl import SYSTEM, USER, call, save_result

ROUNDS = 12
GAP_SECONDS = 10


def main() -> int:
    base = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER}]
    rows: list[dict[str, Any]] = []

    print(f"identical payload, {GAP_SECONDS}s between requests, {ROUNDS} rounds\n")
    print(f"{'#':>3} {'prompt':>7} {'cached':>7} {'hit%':>6} {'gen s':>7} {'gap s':>7}")
    for index in range(ROUNDS):
        result = call(base)
        prompt = result["prompt_tokens"]
        cached = result["cached_tokens"]
        share = f"{100.0 * cached / prompt:.1f}" if prompt and cached is not None else "-"
        rows.append({"round": index, **result})
        print(f"{index:>3} {str(prompt) if prompt is not None else '-':>7} "
              f"{str(cached) if cached is not None else '-':>7} {share:>6} "
              f"{result['seconds']:>7} {GAP_SECONDS if index else 0:>7}")
        if index + 1 < ROUNDS:
            time.sleep(GAP_SECONDS)

    misses = [row["round"] for row in rows if row["cached_tokens"] == 0
              and row["round"] > 0]
    unknown = [row["round"] for row in rows if row["cached_tokens"] is None
               and row["round"] > 0]
    print(f"\nreported zero cached tokens after the first request: {misses or 'none'}")
    print(f"unknown cached tokens after the first request: {unknown or 'none'}")
    print("A reported zero does not establish eviction or its cause.")

    out = Path(__file__).resolve().parents[1] / "docs" / "evaluations" / "cache-gap.json"
    out = save_result(out, {"rounds": ROUNDS, "gap_seconds": GAP_SECONDS,
                            "rows": rows, "misses": misses, "unknown": unknown})
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
