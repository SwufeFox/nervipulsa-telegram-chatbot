"""Report cache usage for identical back-to-back requests.

Serial gap observations cannot rule out TTL: earlier requests may refresh the
cache. Even 20 hits out of 20 cannot exclude multiple replicas, and different
counts or repeated zeros do not identify routing or eviction as their cause.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from experiments.cache_ttl import SYSTEM, USER, call, save_result

ROUNDS = 20


def main() -> int:
    base = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER}]
    hits: list[int | None] = []
    rows: list[dict[str, Any]] = []

    print(f"{'#':>3} {'prompt':>7} {'cached':>7} {'hit%':>6} {'seconds':>8}")
    for index in range(ROUNDS):
        result = call(base)
        prompt = result["prompt_tokens"]
        cached = result["cached_tokens"]
        share = f"{100.0 * cached / prompt:.1f}" if prompt and cached is not None else "-"
        hits.append(cached)
        rows.append({"round": index, **result})
        print(f"{index:>3} {str(prompt) if prompt is not None else '-':>7} "
              f"{str(cached) if cached is not None else '-':>7} {share:>6} {result['seconds']:>8}")

    buckets = Counter(value for value in hits if value is not None)
    unknown = sum(value is None for value in hits)
    misses = sum(1 for value in hits if value == 0)
    print("\ndistinct cached_tokens values:", dict(buckets))
    print(f"reported zero cached tokens: {misses}/{len(hits) - unknown} known; "
          f"{unknown} unknown")
    print("Counts alone do not establish TTL, replica count, routing, or eviction.")

    out = Path(__file__).resolve().parents[1] / "docs" / "evaluations" / "cache-burst.json"
    out = save_result(out, {"rounds": ROUNDS, "rows": rows,
                            "distinct": dict(buckets), "misses": misses, "unknown": unknown})
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
