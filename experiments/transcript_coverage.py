"""Compare text matches in the final projector snapshot for two prompt variants.

The query includes stored bot messages as well as other group messages. Each
nonempty stored text is checked as a substring of final snapshot user-message
content; duplicate or incidental substrings may match. This is not a record of
what the model saw at every request. The delta variant sends only the current
turn's new messages, not all accumulated unseen messages; it cannot establish
whether a more complete incremental approach would retain coverage. The 68%
line-duplication observation excludes system content and whitespace and is not
a token-overhead measurement.
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
from nervipulsa.providers import ScriptedBackend  # noqa: E402

import experiments.long_conversation as L  # noqa: E402
from experiments.cache_ttl import save_result  # noqa: E402


class DeltaPromptAgent(L.NervipulsaAgent):
    """Keep only current-turn messages; this does not accumulate unseen messages."""

    async def ask(self, *, chat_id: int, prompt: str, tag: str) -> Any:
        marker = "--- new messages ---"
        if marker in prompt:
            head, tail = prompt.split(marker, 1)
            # Keep the one-line chat header, drop every rendered history line.
            header = head.strip().splitlines()[0] if head.strip() else ""
            prompt = f"{header}\n{marker}{tail}"
        return await super().ask(chat_id=chat_id, prompt=prompt, tag=tag)


async def run_once(*, delta: bool, turns: int) -> dict[str, Any]:
    async with AsyncExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="coverage-", dir=ROOT)))
        store = L.Store(root / "bot.sqlite3")
        stack.callback(store.close)
        config = L._build_config(root, 24)
        prompt = L.load_profile(config)
        clock = [1_700_000_000.0]

        backend = ScriptedBackend(
            lambda request: ModelResponse(content="收到，我看一眼。", model="scripted")
        )
        agent_class = DeltaPromptAgent if delta else L.NervipulsaAgent
        agent = agent_class(
            settings=L._settings(config), workspace=root / "chat",
            system_prompt=prompt, timeout=config.request_timeout, backend=backend,
        )
        stack.push_async_callback(agent.stop)
        bot = L.GroupAgent(
            config=config, store=store, client=L.TelegramClient(L.RecordingTransport()),
            system_prompt=prompt, agent_factory=lambda spec: agent,
            turn_manager=L.TurnManager(debounce_seconds=config.debounce_seconds,
                                       clock=lambda: clock[0]),
            bot_user_id=L.fixtures.ALMA, clock=lambda: clock[0],
        )

        stack.push_async_callback(bot.stop)
        await agent.start()
        await bot.start()
        message_id = 1000
        for index, utterance in enumerate(L.SCRIPT[:turns]):
            message_id += 1
            await bot.step([_update(index + 1, message_id, utterance)])
            clock[0] += config.debounce_seconds + 0.1
            await bot.run_turns()

        messages = agent.runtime.actor.projector.snapshot() if agent.runtime else []
        user_texts = [m.get("content") or "" for m in messages
                      if m.get("role") == "user"]

        rows = store._query(
            "SELECT message_id, text FROM messages WHERE chat_id = ? ORDER BY message_id",
            (L.fixtures.TEST_CHAT,),
        )
        utterances = [row["text"] for row in rows if row["text"].strip()]
        # Substring matches in the final snapshot are only a coverage proxy.
        # The stored rows also include bot messages.
        visible = sum(1 for text in utterances
                      if any(text in user_text for user_text in user_texts))

    return {
        "mode": "delta" if delta else "full",
        "turns": turns,
        "transcript_user_messages": len(user_texts),
        "transcript_chars": sum(len(text) for text in user_texts),
        "group_utterances": len(utterances),
        "visible_to_model": visible,
        "invisible_to_model": len(utterances) - visible,
    }


def _update(update_id: int, message_id: int, utterance) -> dict:
    return L._update(update_id, message_id, utterance, None)


def main() -> int:
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else len(L.SCRIPT)
    full = asyncio.run(run_once(delta=False, turns=turns))
    delta = asyncio.run(run_once(delta=True, turns=turns))

    print("Final projector snapshot substring matches; stored texts include bot messages.")
    print("Delta means current turn only, not accumulated unseen messages.")
    print(f"{'mode':<6} {'userMsgs':>9} {'chars':>8} {'utterances':>11} "
          f"{'matched':>8} {'unmatched':>10}")
    for row in (full, delta):
        print(f"{row['mode']:<6} {row['transcript_user_messages']:>9} "
              f"{row['transcript_chars']:>8} {row['group_utterances']:>11} "
              f"{row['visible_to_model']:>8} {row['invisible_to_model']:>10}")

    print()
    print(f"full: {full['visible_to_model']}/{full['group_utterances']} stored texts match")
    print(f"current-turn-only: {delta['visible_to_model']}/{delta['group_utterances']} stored texts match")

    out = ROOT / "docs" / "evaluations" / "transcript-coverage.json"
    out = save_result(out, {"full": full, "delta": delta})
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
