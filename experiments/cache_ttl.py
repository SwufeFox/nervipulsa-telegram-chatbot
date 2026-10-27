"""Observe reported cache usage for repeated requests at serial intervals.

Each call may refresh or populate a cache, and request duration adds to the
interval. These observations cannot determine a TTL or rule one out. A zero
cached-token count does not identify the cause. The content-only cache probe
does not establish HTTP byte identity. Run: python experiments/cache_ttl.py
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

MODEL = "opencode-go/muse-spark-1.3-contributor"
URL = "http://127.0.0.1:3425/v1/chat/completions"

# A prefix long enough to be worth caching, and stable across calls.
SYSTEM = (
    "You are a bot in a Telegram group chat. You never pretend to be human. "
    "Answer in one short sentence.\n\n"
    + "背景：这是一个用于测试前缀缓存的固定系统提示，内容故意足够长。" * 20
)
USER = "用一句话说明 redis 的 PERSIST 做什么。"


def call(messages: list[dict[str, str]], *, max_tokens: int = 512) -> dict[str, Any]:
    key = os.environ.get("NERVIPULSA_API_KEY")
    if not key:
        raise RuntimeError("NERVIPULSA_API_KEY is required")
    payload = {"model": MODEL, "messages": messages, "max_tokens": max_tokens}
    request = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=600) as response:
        body = json.loads(response.read())
    elapsed = time.monotonic() - started
    usage = body.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "cached_tokens": details.get("cached_tokens"),
        "cache_write_tokens": details.get("cache_write_tokens"),
        "seconds": round(elapsed, 1),
    }


def save_result(path: Path, data: Any) -> Path:
    """Write a new timestamped result without replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    while True:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = path.with_name(f"{path.stem}-{timestamp}-{uuid4().hex}{path.suffix}")
        try:
            with out.open("x", encoding="utf-8") as result_file:
                result_file.write(text)
            return out
        except FileExistsError:
            continue


def report(label: str, result: dict[str, Any]) -> None:
    prompt = result["prompt_tokens"]
    cached = result["cached_tokens"]
    share = f"{100.0 * cached / prompt:.1f}%" if prompt and cached is not None else "-"
    print(f"{label:<28} prompt {str(prompt) if prompt is not None else '-':>6}  cached {str(cached) if cached is not None else '-':>6}  {share:>7}  "
          f"write {str(result['cache_write_tokens']) if result['cache_write_tokens'] is not None else '-':>6}  {result['seconds']}s")


def main() -> int:
    base = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER}]
    rows: list[dict[str, Any]] = []

    print("== identical payload, increasing gap ==")
    report("first observed request", call(base))

    gaps = [0, 5, 30, 60, 120, 300]
    for gap in gaps:
        if gap:
            print(f"   ... sleeping {gap}s")
            time.sleep(gap)
        result = call(base)
        report(f"identical after {gap}s", result)
        rows.append({"phase": "identical", "gap_seconds": gap, **result})

    print("\n== reported usage with changed prefix ==")
    changed = [{"role": "system", "content": SYSTEM + "\n追加一行。"},
               {"role": "user", "content": USER}]
    report("system changed by one line", call(changed))

    print("\n== reported usage with appended messages ==")
    extended = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER},
        {"role": "assistant", "content": "PERSIST 取消 key 的过期时间。"},
        {"role": "user", "content": "那 EXPIRE 0 呢？"},
    ]
    report("same prefix + 2 more msgs", call(extended))

    out = Path(__file__).resolve().parents[1] / "docs" / "evaluations" / "cache-ttl.json"
    out = save_result(out, {"model": MODEL, "rows": rows})
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
