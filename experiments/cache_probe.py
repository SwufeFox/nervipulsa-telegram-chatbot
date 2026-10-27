"""Compare captured message content across consecutive requests.

The probe compares content strings, not complete messages or serialized HTTP
bytes: roles, other fields, serialization and provider-side processing are not
compared. Content prefix changes may affect caching, but do not establish why a
request hits or misses. Provider-reported usage is shown when available.
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

from nervipulsa.ports import ModelResponse  # noqa: E402
from nervipulsa.providers import OpenAICompatibleBackend  # noqa: E402

import experiments.long_conversation as L  # noqa: E402
from experiments.cache_ttl import save_result  # noqa: E402


class CapturingBackend(OpenAICompatibleBackend):
    """Records selected message fields, then defers to the real provider."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.snapshots: list[dict[str, Any]] = []

    async def complete(self, request):
        messages = [
            {"role": m.get("role"), "content": m.get("content") or ""}
            for m in request.messages
        ]
        self.snapshots.append({
            "activation_id": request.activation_id,
            "messages": messages,
            "chars": sum(len(m["content"]) for m in messages),
        })
        return await super().complete(request)


def common_prefix_len(a: list[str], b: list[str]) -> int:
    """Count equal content strings from the start of two message lists."""
    count = 0
    for left, right in zip(a, b):
        if left != right:
            return count
        count += 1
    return count


def common_char_prefix(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    index = 0
    while index < limit and a[index] == b[index]:
        index += 1
    return index


async def run(turns: int) -> dict[str, Any]:
    async with AsyncExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="cache-probe-", dir=ROOT)))
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
            settings=settings, workspace=root / "chat",
            system_prompt=prompt, timeout=config.request_timeout, backend=backend,
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
            await bot.step([L._update(index + 1, message_id, utterance, None)])
            clock[0] += config.debounce_seconds + 0.1
            before = len(backend.snapshots)
            decisions = await bot.run_turns()

            stored = L._new_decisions(store, seen)
            usage: dict[str, Any] = {}
            for row in stored:
                activation = row["detail"].get("activation") or {}
                if activation.get("usage"):
                    usage = activation["usage"]
            if len(backend.snapshots) == before:
                continue

            snap = backend.snapshots[-1]
            rows.append({
                "turn": index,
                "messages": len(snap["messages"]),
                "chars": snap["chars"],
                "prompt_tokens": usage.get("prompt_tokens"),
                "cached_tokens": (
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                ),
                "content": [m["content"] for m in snap["messages"]],
            })

    # Compare each request with the one before it.
    for previous, current in zip(rows, rows[1:]):
        shared = common_prefix_len(previous["content"], current["content"])
        current["shared_messages"] = shared
        current["total_messages"] = len(current["content"])
        if shared:
            current["shared_chars"] = common_char_prefix(
                "".join(previous["content"][:shared]),
                "".join(current["content"][:shared]),
            )
        else:
            current["shared_chars"] = 0
        first = current["content"][0] if current["content"] else ""
        prev_first = previous["content"][0] if previous["content"] else ""
        current["system_identical"] = first == prev_first
        current["first_user_identical"] = (
            len(current["content"]) > 1 and len(previous["content"]) > 1
            and current["content"][1] == previous["content"][1]
        )

    return {"rows": rows, "workspace": str(root)}


def main() -> int:
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else 49
    result = asyncio.run(run(turns))
    rows = result["rows"]

    print("Comparisons cover message content only, not HTTP bytes or other fields.")
    print(f"{'turn':>4} {'msgs':>5} {'chars':>7} {'prompt':>7} {'cached':>7} "
          f"{'hit%':>6} {'sharedMsgs':>10} {'sys=':>5} {'firstUser=':>10}")
    for index, row in enumerate(rows):
        hit = (f"{100.0 * row['cached_tokens'] / row['prompt_tokens']:.1f}"
               if row["prompt_tokens"] and row["cached_tokens"] is not None else "-")
        shared = row.get("shared_messages", "-")
        sys_same = row.get("system_identical", "-")
        fu = row.get("first_user_identical", "-")
        print(f"{row['turn']:>4} {row['messages']:>5} {row['chars']:>7} "
              f"{str(row['prompt_tokens']) if row['prompt_tokens'] is not None else '-':>7} "
              f"{str(row['cached_tokens']) if row['cached_tokens'] is not None else '-':>7} {hit:>6} "
              f"{shared!s:>10} {sys_same!s:>5} {fu!s:>10}")

    out = Path(ROOT / "docs" / "evaluations" / "cache-probe.json")
    # Only the measurement, not the chat text, is kept in the report.
    slim = [{k: v for k, v in row.items() if k != "content"} for row in rows]
    out = save_result(out, {"rows": slim})
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
