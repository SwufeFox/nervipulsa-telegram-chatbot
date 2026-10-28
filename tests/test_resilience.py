"""Resilience: one bad turn, or one bad poll cycle, must not stop the bot.

Both of these were found by probing the code after the first 101 tests were
already green. That is the point of this file: the original suite tested that
things *work*, and nothing tested that one failure stays contained.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import fixtures
from app.agent import ScriptedAgent
from app.bot import GroupAgent
from app.store import Store
from app.telegram import TelegramClient
from app.turns import TurnManager
from conftest import FakeTransport, run


def make_bot(store, config, transport, clock, system_prompt, responder=None):
    agent = ScriptedAgent(responder or (lambda prompt: "a reply"))
    bot = GroupAgent(
        legacy_policy=True,
        config=config, store=store, client=TelegramClient(transport),
        system_prompt=system_prompt, agent_factory=lambda spec: agent,
        turn_manager=TurnManager(debounce_seconds=2.0, clock=clock),
        bot_user_id=fixtures.ALMA, clock=clock,
    )
    return bot, agent


def topic_update(update_id: int, message_id: int, topic_id: int, text: str) -> dict:
    return fixtures.message(update_id=update_id, message_id=message_id,
                            sender_id=fixtures.ALICE, username="alice",
                            text=text, topic_id=topic_id)


def test_one_failing_turn_does_not_take_the_batch_down(store, config, transport,
                                                       clock, system_prompt) -> None:
    """Three turns, the middle one raises. The other two must still be decided."""
    bot, agent = make_bot(store, config, transport, clock, system_prompt)
    original = bot._process_turn
    attempted: list[int | None] = []

    async def flaky(turn, tick):
        attempted.append(turn.topic_id)
        if turn.topic_id == 22:
            raise RuntimeError("simulated storage error on this turn")
        return await original(turn, tick)

    bot._process_turn = flaky

    async def body() -> None:
        await bot.start()
        await bot.step([
            topic_update(1, 1, 11, "Alma one"),
            topic_update(2, 2, 22, "Alma two"),
            topic_update(3, 3, 33, "Alma three"),
        ])
        clock.advance(3.0)
        decisions = await bot.run_turns()   # must not raise

        assert sorted(attempted) == [11, 22, 33], "the batch stopped early"
        assert len(decisions) == 2, f"{len(decisions)} decisions (2 expected)"
        assert len(transport.sends) == 2
        # The failure is visible in the store, not swallowed.
        rows = store._query("SELECT reason FROM decisions")
        assert "turn_failed" in [r["reason"] for r in rows], [dict(r) for r in rows]
        failed_turns = store._query("SELECT outcome FROM turns WHERE outcome = 'error'")
        assert len(failed_turns) == 1
        assert bot.stats.total.errors >= 1
        await bot.stop()

    run(body())


def test_a_failed_turn_is_not_replayed_on_restart(store, config, transport,
                                                  clock, system_prompt) -> None:
    """The failed window is claimed, so recovery does not answer it twice."""
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([topic_update(1, 1, 11, "Alma one")])
        clock.advance(3.0)
        turn = bot.turns.ready(now=clock())[0]
        bot._record_failed_turn(turn, bot.stats.total)
        assert store.unanswered_messages() == [], "a failed turn was left re-openable"
        await bot.stop()

    run(body())


def test_poll_once_survives_a_failing_turn(store, config, transport, clock,
                                           system_prompt) -> None:
    """The live loop calls poll_once; it must not propagate a turn failure."""
    bot, agent = make_bot(store, config, transport, clock, system_prompt)
    transport.updates = [[topic_update(1, 1, 11, "Alma one")]]

    async def failing(turn, tick):
        raise RuntimeError("boom")

    bot._process_turn = failing

    async def body() -> None:
        await bot.start()
        offset, tick = await bot.poll_once(offset=None)   # must not raise
        assert offset == 2
        assert tick.messages_stored == 1
        # The window was still open during that poll, so no turn ran yet.
        assert tick.errors == 0
        # Close the window; the failing turn must be contained here too.
        clock.advance(3.0)
        offset, tick = await bot.poll_once(offset=offset)  # must not raise
        assert tick.errors == 1
        assert transport.sends == []
        await bot.stop()

    run(body())


def test_poll_once_survives_a_transport_failure(store, config, clock,
                                                system_prompt) -> None:
    class FlakyTransport(FakeTransport):
        def call(self, method, payload):
            if method == "getUpdates":
                raise ConnectionResetError("network blip")
            return super().call(method, payload)

    bot, _ = make_bot(store, config, FlakyTransport(), clock, system_prompt)

    async def body() -> None:
        await bot.start()
        offset, tick = await bot.poll_once(offset=42)
        assert offset == 42, "the cursor moved despite a failed fetch"
        assert tick.errors == 1
        await bot.stop()

    run(body())


def test_open_turn_is_idempotent(store) -> None:
    """Re-opening a turn must not reset its outcome or timestamp."""
    store.open_turn(turn_id="t1", chat_id=fixtures.TEST_CHAT, topic_id=None,
                    first_message=1, last_message=5, trigger="mention")
    store.close_turn("t1", "sent")
    before = dict(store._query("SELECT * FROM turns WHERE turn_id='t1'")[0])

    # A second open (recovery, retry, a bug) must be a no-op.
    store.open_turn(turn_id="t1", chat_id=fixtures.TEST_CHAT, topic_id=None,
                    first_message=1, last_message=5, trigger="proactive")
    after = dict(store._query("SELECT * FROM turns WHERE turn_id='t1'")[0])

    assert after["outcome"] == "sent", "open_turn reset a completed turn"
    assert after["closed_at"] == before["closed_at"]
    assert after["trigger"] == before["trigger"]


def test_a_turn_that_fails_mid_send_is_recorded_not_lost(store, config, transport,
                                                         clock, system_prompt) -> None:
    """If the store itself throws while claiming a send, the turn is recorded."""
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    def broken_prepare(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    store.prepare_send = broken_prepare  # type: ignore[assignment]

    async def body() -> None:
        await bot.start()
        await bot.step([topic_update(1, 1, 11, "Alma one")])
        clock.advance(3.0)
        decisions = await bot.run_turns()   # must not raise
        assert decisions == []
        assert transport.sends == [], "a send happened without a durable intent"
        rows = store._query("SELECT reason FROM decisions")
        assert "turn_failed" in [r["reason"] for r in rows], [dict(r) for r in rows]
        assert store.stats()["turns"] == 1
        await bot.stop()

    run(body())
