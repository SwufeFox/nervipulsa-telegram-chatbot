"""An episode whose work is done must not adopt unrelated later conversation.

The delivery, the goal resolution and the episode close are three separate
writes. A process that dies between them leaves an open episode whose job is
already finished. On restart nothing looks wrong - the episode is simply
"open" - so the next message in that topic joins it and is rendered as a
continuation of work that ended before it was ever sent. The identity is
durable; what it claims must be re-derived from durable facts, not assumed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import fixtures
from conftest import run

from test_episode_continuation import build, call, msg


def test_reply_that_says_it_completes_a_goal_still_recovers_after_restart(tmp_path):
    """A restart must not resume work the previous process already delivered."""
    first = tmp_path / "app.db"

    def answer(index, payload):
        if index == 1:
            from nervipulsa.ports import ModelResponse
            from nervipulsa.ports import ToolCall
            # One activation that opens a goal and then claims to finish it.
            return ModelResponse(
                content="", model="scripted",
                tool_calls=[
                    ToolCall(id="c-intent", name="set_intent",
                             arguments={"goal": "查清楚 502 的根因"}),
                    ToolCall(id="c-send", name="send_message",
                             arguments={"text": "查完了，是网关超时",
                                        "resolves_intent": True}),
                ],
            )
        return call("finish_silently")

    async def crash_after_delivery():
        bot, store, _clock, transport, _payloads = build(tmp_path, answer)
        # The process dies between the two writes: delivery resolves the goal
        # in its own transaction, and the episode close never runs.
        bot.autonomy._settle_episode = lambda *a, **k: None
        await bot.start()
        try:
            await bot.step([msg(201, "502 到底怎么回事")])
            await bot.autonomous_step(force=True)
            assert transport.sends and transport.sends[0]["text"] == "查完了，是网关超时"
            scope = (fixtures.TEST_CHAT, fixtures.TOPIC_A)
            assert store.current_intent(*scope) is None
            stale = store.current_episode(*scope)
            assert stale is not None
            return str(stale["episode_id"])
        finally:
            await bot.stop()

    stale_id = run(crash_after_delivery())

    def fresh(index, payload):
        return call("finish_silently")

    async def restart():
        bot, store, _clock, _transport, payloads = build(tmp_path, fresh)
        scope = (fixtures.TEST_CHAT, fixtures.TOPIC_A)
        assert store.current_episode(*scope) is not None  # as a crash left it
        await bot.start()
        try:
            # Startup reconciles, so an unrelated message is not absorbed into
            # a job that finished before this message existed.
            assert store.current_episode(*scope) is None
            await bot.step([msg(202, "今天午饭吃什么")])
            await bot.autonomous_step(force=True)
            rendered = payloads[-1]
            assert rendered.get("open_intent") is None
            episode = rendered.get("episode")
            # A fresh episode is fine; what must not happen is this message
            # being rendered as the continuation of the completed job.
            assert episode is None or episode["episode_id"] != stale_id
            assert not (episode or {}).get("continuation")
        finally:
            await bot.stop()

    run(restart())
