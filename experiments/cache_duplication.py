"""Measure repeated stripped nonempty lines in captured message content.

The calculation excludes the first (system) message, strips leading and trailing
whitespace, and counts characters of nonempty lines, including repetitions
within a single message. It does not measure token overhead or total request
size; the separate total_chars field includes the system message and whitespace.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import experiments.long_conversation as L  # noqa: E402
from experiments.cache_ttl import save_result  # noqa: E402
from experiments.cache_probe import CapturingBackend  # noqa: E402


def line_duplication(contents: list[str]) -> dict[str, int]:
    """Count characters in stripped nonempty lines, including within-message repeats."""
    seen: set[str] = set()
    unique = 0
    repeated = 0
    for content in contents:
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped in seen:
                repeated += len(stripped)
            else:
                seen.add(stripped)
                unique += len(stripped)
    return {"unique_chars": unique, "repeated_chars": repeated}


async def run(turns: int) -> list[dict[str, Any]]:
    async with AsyncExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="dup-probe-", dir=ROOT)))
        store = L.Store(root / "bot.sqlite3")
        stack.callback(store.close)
        transport = L.RecordingTransport()
        config = L._build_config(root, 24)
        prompt = L.load_profile(config)
        clock = [1_700_000_000.0]

        settings = L._settings(config)
        backend = CapturingBackend(
            provider=settings.provider, base_url=settings.base_url,
            api_key=settings.api_key, model=settings.model,
            timeout=config.request_timeout, adapter=settings.adapter,
        )
        agent = L.NervipulsaAgent(
            settings=settings, workspace=root / "chat", system_prompt=prompt,
            timeout=config.request_timeout, backend=backend,
        )
        stack.push_async_callback(agent.stop)
        bot = L.GroupAgent(
            config=config, store=store, client=L.TelegramClient(transport),
            system_prompt=prompt, agent_factory=lambda spec: agent,
            turn_manager=L.TurnManager(debounce_seconds=config.debounce_seconds,
                                       clock=lambda: clock[0]),
            bot_user_id=L.fixtures.ALMA, clock=lambda: clock[0],
        )

        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        stack.push_async_callback(bot.stop)
        await agent.start()
        await bot.start()
        L._new_decisions(store, seen)
        message_id = 1000
        for index, utterance in enumerate(L.SCRIPT[:turns]):
            message_id += 1
            await bot.step([_update(index + 1, message_id, utterance)])
            clock[0] += config.debounce_seconds + 0.1
            before = len(backend.snapshots)
            await bot.run_turns()

            stored = L._new_decisions(store, seen)
            usage: dict[str, Any] = {}
            for row in stored:
                activation = row["detail"].get("activation") or {}
                if activation.get("usage"):
                    usage = activation["usage"]
            if len(backend.snapshots) == before:
                continue

            snap = backend.snapshots[-1]
            contents = [m["content"] for m in snap["messages"]]
            stats = line_duplication(contents[1:])  # skip the system message
            rows.append({
                "turn": index,
                "messages": len(contents),
                "total_chars": sum(len(c) for c in contents),
                **stats,
                "prompt_tokens": usage.get("prompt_tokens"),
                "cached_tokens": (
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                ),
            })
    return rows


def _update(update_id: int, message_id: int, utterance) -> dict[str, Any]:
    return L._update(update_id, message_id, utterance, None)


def main() -> int:
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else 49
    rows = asyncio.run(run(turns))

    print("Duplication counts stripped nonempty line characters excluding system; not tokens.")
    print(f"{'turn':>4} {'msgs':>5} {'chars':>7} {'unique':>7} {'repeat':>7} "
          f"{'dup%':>6} {'prompt':>7} {'cached':>7}")
    for row in rows:
        total = row["unique_chars"] + row["repeated_chars"]
        dup = 100.0 * row["repeated_chars"] / total if total else 0.0
        print(f"{row['turn']:>4} {row['messages']:>5} {row['total_chars']:>7} "
              f"{row['unique_chars']:>7} {row['repeated_chars']:>7} {dup:>6.1f} "
              f"{str(row['prompt_tokens']) if row['prompt_tokens'] is not None else '-':>7} "
              f"{str(row['cached_tokens']) if row['cached_tokens'] is not None else '-':>7}")

    if rows:
        last = rows[-1]
        total = last["unique_chars"] + last["repeated_chars"]
        print()
        print(f"last request: {total} stripped nonempty line chars excluding system, "
              f"{last['unique_chars']} unique, {last['repeated_chars']} repeated")
        print(f"line-character duplication: {100.0 * last['repeated_chars'] / total:.1f}%"
              if total else "line-character duplication: unavailable (no counted characters)")
        print(f"prompt tokens: {last['prompt_tokens']}, "
              f"cached: {last['cached_tokens']}")

    out = ROOT / "docs" / "evaluations" / "cache-duplication.json"
    out = save_result(out, {"rows": rows})
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
