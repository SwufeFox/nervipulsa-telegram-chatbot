"""Paired latency probe for cached vs unique-prefix Magpie requests.

This isolates provider request latency from Telegram and the application loop.
It stores only usage counters, timings, and request hashes; prompt text and the
API key are never written to the report.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.main import build_settings  # noqa: E402

FILLER = (
    "This sentence is fixed synthetic filler for measuring prompt-cache prefill latency. "
    "It contains no facts and must not be analyzed or summarized.\n"
)
COLD_PREFIXES = "ABCDEFGHJKLMNPQRSTUVWXYZ"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_body(model: str, salt: str, repetitions: int, max_tokens: int) -> bytes:
    system = (
        f"{salt}\n"
        "This is an opaque benchmark label; ignore it. This request measures prompt "
        "prefill only. Reply with exactly OK.\n"
        + FILLER * repetitions
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Reply with exactly OK."},
        ],
        "max_tokens": max_tokens,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def int_or_none(value: Any) -> int | None:
    return value if type(value) is int else None


def send_request(
    *, url: str, api_key: str, body: bytes, timeout: float,
    condition: str, pair_index: int | None, order_index: int,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    started = time.perf_counter()
    row: dict[str, Any] = {
        "condition": condition,
        "pair_index": pair_index,
        "order_index": order_index,
        "request_sha256": hashlib.sha256(body).hexdigest(),
        "request_bytes": len(body),
        "status": "started",
    }
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            row["http_status"] = response.status
            payload = json.loads(response.read())
        usage = payload.get("usage") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        row.update({
            "status": "ok",
            "prompt_tokens": int_or_none(usage.get("prompt_tokens")),
            "cached_tokens": int_or_none(prompt_details.get("cached_tokens")),
            "completion_tokens": int_or_none(usage.get("completion_tokens")),
            "reasoning_tokens": int_or_none(completion_details.get("reasoning_tokens")),
            "usage_present": isinstance(usage, dict) and "prompt_tokens" in usage,
        })
    except urllib.error.HTTPError as exc:
        row.update({"status": "http_error", "http_status": exc.code})
    except Exception as exc:  # do not persist exception text; it may contain request details
        row.update({"status": "error", "error_type": type(exc).__name__})
    finally:
        row["elapsed_seconds"] = round(time.perf_counter() - started, 4)
    return row


def percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    index = max(0, min(len(sorted_values) - 1, math.ceil(p * len(sorted_values)) - 1))
    return sorted_values[index]


def paired_statistics(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [
        pair for pair in pairs
        if pair["warm"].get("status") == "ok"
        and pair["unique_prefix"].get("status") == "ok"
        and (pair["warm"].get("cached_tokens") or 0) > 0
        and pair["unique_prefix"].get("cached_tokens") == 0
    ]
    deltas = [
        pair["unique_prefix"]["elapsed_seconds"] - pair["warm"]["elapsed_seconds"]
        for pair in valid
    ]
    reductions = [
        (pair["unique_prefix"]["elapsed_seconds"] - pair["warm"]["elapsed_seconds"])
        / pair["unique_prefix"]["elapsed_seconds"] * 100
        for pair in valid if pair["unique_prefix"]["elapsed_seconds"] > 0
    ]
    warm = [pair["warm"]["elapsed_seconds"] for pair in valid]
    unique = [pair["unique_prefix"]["elapsed_seconds"] for pair in valid]
    non_ties = [delta for delta in deltas if delta != 0]
    wins = sum(delta > 0 for delta in non_ties)
    n = len(non_ties)
    if n:
        tail = sum(math.comb(n, i) for i in range(min(wins, n - wins) + 1))
        sign_test_two_sided_p = min(1.0, 2 * tail / (2 ** n))
    else:
        sign_test_two_sided_p = None
    bootstrap_ci = None
    if deltas:
        rng = random.Random(20261030)
        boot = sorted(
            statistics.median(rng.choice(deltas) for _ in range(len(deltas)))
            for _ in range(5000)
        )
        bootstrap_ci = [percentile(boot, 0.025), percentile(boot, 0.975)]
    prompt_diffs = [
        abs((pair["warm"].get("prompt_tokens") or 0) - (pair["unique_prefix"].get("prompt_tokens") or 0))
        for pair in valid
    ]
    completion_diffs = [
        abs((pair["warm"].get("completion_tokens") or 0) - (pair["unique_prefix"].get("completion_tokens") or 0))
        for pair in valid
    ]
    return {
        "pairs_requested": len(pairs),
        "pairs_validated": len(valid),
        "cache_controls_validated": len(valid) == len(pairs) and bool(pairs),
        "warm_median_seconds": round(statistics.median(warm), 4) if warm else None,
        "unique_prefix_median_seconds": round(statistics.median(unique), 4) if unique else None,
        "median_paired_latency_saved_seconds": round(statistics.median(deltas), 4) if deltas else None,
        "median_paired_latency_reduction_percent": round(statistics.median(reductions), 2) if reductions else None,
        "warm_faster_pairs": sum(delta > 0 for delta in deltas),
        "unique_prefix_faster_pairs": sum(delta < 0 for delta in deltas),
        "ties": sum(delta == 0 for delta in deltas),
        "sign_test_two_sided_p": round(sign_test_two_sided_p, 6) if sign_test_two_sided_p is not None else None,
        "bootstrap_95pct_ci_median_saved_seconds": [round(v, 4) if v is not None else None for v in bootstrap_ci] if bootstrap_ci else None,
        "median_abs_prompt_token_difference": round(statistics.median(prompt_diffs), 1) if prompt_diffs else None,
        "median_abs_completion_token_difference": round(statistics.median(completion_diffs), 1) if completion_diffs else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=12)
    parser.add_argument("--filler-repetitions", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=32)
    args = parser.parse_args()
    if not 4 <= args.pairs <= len(COLD_PREFIXES):
        parser.error(f"--pairs must be 4..{len(COLD_PREFIXES)}")
    if args.filler_repetitions < 1 or args.max_tokens < 1:
        parser.error("filler repetitions and max tokens must be positive")

    config = load_config(ROOT / "config.fox.toml")
    settings = build_settings(config)
    api_key = settings.api_key
    if not api_key:
        raise SystemExit("model authentication is not configured")
    url = settings.base_url.rstrip("/") + "/chat/completions"
    model = settings.model
    run_id = uuid.uuid4().hex
    warm_salt = "0" + hashlib.sha256((run_id + ":warm").encode()).hexdigest()[:31]
    control_salt = "1" + hashlib.sha256((run_id + ":control").encode()).hexdigest()[:31]
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = ROOT / "docs" / "evaluations" / f"cache-speed-ab-{run_stamp}-{run_id[:8]}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "started_at_utc": utc_now(),
        "model": model,
        "provider": settings.provider,
        "endpoint_base": settings.base_url,
        "prompt_spec": {
            "filler_repetitions": args.filler_repetitions,
            "max_tokens": args.max_tokens,
            "pairs_requested": args.pairs,
            "warm_condition": "same synthetic request body, warmed by three unmeasured calls",
            "control_condition": "same-length unique first-token salt for each request; must report zero cached tokens",
            "raw_prompt_saved": False,
            "api_key_saved": False,
        },
        "warmups": [],
        "unique_prefix_probe": None,
        "pairs": [],
        "analysis": None,
        "limitations": [
            "Measures complete model endpoint round-trip, not application or Telegram latency.",
            "The unique-prefix control changes a 32-character synthetic salt; cache miss must be confirmed by usage counters.",
            "Output generation and gateway load can still affect latency; completion token counts are retained for comparison.",
            "Results apply to this prompt size and endpoint, not all production conversations.",
        ],
    }

    def save() -> None:
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    save()  # durable starting point before any model requests
    order_index = 0
    try:
        warm_body = request_body(model, warm_salt, args.filler_repetitions, args.max_tokens)
        for _ in range(3):
            order_index += 1
            row = send_request(url=url, api_key=api_key, body=warm_body,
                               timeout=config.request_timeout, condition="warmup",
                               pair_index=None, order_index=order_index)
            report["warmups"].append(row)
            save()
            if row["status"] != "ok":
                report["status"] = "request_error"
                report["error_type"] = row.get("error_type", row.get("http_status"))
                save()
                return 1
            print(f"warmup {len(report['warmups'])}/3: {row['elapsed_seconds']:.2f}s cached={row.get('cached_tokens')}", flush=True)

        control_body = request_body(model, control_salt, args.filler_repetitions, args.max_tokens)
        order_index += 1
        control_probe = send_request(url=url, api_key=api_key, body=control_body,
                                     timeout=config.request_timeout, condition="control_probe",
                                     pair_index=None, order_index=order_index)
        report["unique_prefix_probe"] = control_probe
        save()
        if control_probe["status"] != "ok":
            report["status"] = "request_error"
            report["error_type"] = control_probe.get("error_type", control_probe.get("http_status"))
            save()
            return 1
        if not any((row.get("cached_tokens") or 0) > 0 for row in report["warmups"][1:]):
            report["status"] = "warm_cache_not_verified"
            save()
            print("warm cache not verified; stopping before paired run", flush=True)
            return 2
        if control_probe.get("cached_tokens") != 0:
            report["status"] = "unique_prefix_miss_not_verified"
            save()
            print("unique-prefix control still reported cached tokens; stopping", flush=True)
            return 2

        rng = random.Random(81533)
        for pair_index in range(args.pairs):
            cold_salt = COLD_PREFIXES[pair_index] + hashlib.sha256(
                f"{run_id}:cold:{pair_index}".encode()
            ).hexdigest()[:31]
            cold_body = request_body(model, cold_salt, args.filler_repetitions, args.max_tokens)
            order = ["warm", "unique_prefix"]
            rng.shuffle(order)
            pair: dict[str, Any] = {"pair_index": pair_index + 1, "order": order}
            for condition in order:
                order_index += 1
                body = warm_body if condition == "warm" else cold_body
                row = send_request(
                    url=url, api_key=api_key, body=body,
                    timeout=config.request_timeout, condition=condition,
                    pair_index=pair_index + 1, order_index=order_index,
                )
                pair[condition] = row
                save()
                if row["status"] != "ok":
                    report["status"] = "request_error"
                    report["error_type"] = row.get("error_type", row.get("http_status"))
                    report["analysis"] = paired_statistics(report["pairs"])
                    save()
                    return 1
            report["pairs"].append(pair)
            report["analysis"] = paired_statistics(report["pairs"])
            save()
            warm = pair["warm"]
            cold = pair["unique_prefix"]
            print(
                f"pair {pair_index + 1}/{args.pairs}: warm={warm['elapsed_seconds']:.2f}s "
                f"({warm.get('cached_tokens')} cached), unique={cold['elapsed_seconds']:.2f}s "
                f"({cold.get('cached_tokens')} cached)",
                flush=True,
            )
        report["analysis"] = paired_statistics(report["pairs"])
        report["status"] = "completed" if report["analysis"]["cache_controls_validated"] else "controls_not_validated"
        report["finished_at_utc"] = utc_now()
        save()
    except Exception as exc:
        report["status"] = "runner_error"
        report["error_type"] = type(exc).__name__
        report["finished_at_utc"] = utc_now()
        save()
        print(f"benchmark stopped: {type(exc).__name__}", file=sys.stderr, flush=True)
        return 1

    print(json.dumps({
        "status": report["status"],
        "report": str(output_path.relative_to(ROOT)),
        "analysis": report["analysis"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
