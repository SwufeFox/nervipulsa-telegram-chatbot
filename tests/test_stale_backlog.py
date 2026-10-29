"""A long outage must not make the agent answer yesterday's conversation.

Re-arming pending observations on restart exists so a message received just
before a crash still gets answered. That guarantee is about seconds and
minutes. Applied without an age limit it also replays hours of backlog, and the
agent then reasons about a conversation that ended long ago while the chat has
moved on - and speaks into the present one.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import fixtures
from conftest import run

from test_autonomous_budgets_and_replay import build, msg, silent


def age_observations(store, *, older_than: float) -> None:
    """Backdate every stored observation, standing in for a long outage.

    The timestamps are set far in the past relative to any clock a later
    `Store` might use: the restart in these tests constructs its own clock, so
    writing "now minus N" from the current one would not survive it. What the
    rule checks is that the observation is much older than `older_than`, not
    which particular instant it was.
    """
    stamp = 1_000_000_000.0 - older_than
    with store._write() as conn:
        conn.execute("UPDATE observation_events SET received_at = ?", (stamp,))


def test_restart_skips_a_backlog_that_ended_hours_ago(tmp_path):
    db = tmp_path / "app.db"

    async def crash():
        bot, store, _clock, _transport, _handle = build(tmp_path, db=db)
        await bot.start()
        await bot.step([msg(801, "二十小时前的抽卡"), msg(802, "抽到逆位了")])
        await bot.stop()
        store.close()

    run(crash())

    from app.store import Store
    store = Store(db)
    age_observations(store, older_than=20 * 3600)
    store.close()

    async def restart():
        calls = []

        def responder(request):
            calls.append(1)
            return silent(request)

        bot, store, clock, _transport, _handle = build(tmp_path, responder, db=db)
        await bot.start()
        try:
            # The stale pair is marked read, so nothing is queued to answer it.
            assert bot.recovered_inbound == 0
            assert not bot.queued_topics(), "a 20-hour-old backlog was re-armed"
            clock.advance(10)
            results = await bot.autonomous_step(force=False)
            assert results == []
            assert calls == [], "the model was asked about yesterday's chat"
        finally:
            await bot.stop()
            store.close()

    run(restart())


def test_restart_still_answers_what_it_just_missed(tmp_path):
    """The age limit must not swallow the message the guarantee was for."""
    db = tmp_path / "app.db"

    async def crash():
        bot, store, _clock, _transport, _handle = build(tmp_path, db=db)
        await bot.start()
        await bot.step([msg(811, "刚发的，进程就挂了")])
        await bot.stop()
        store.close()

    run(crash())

    async def restart():
        calls = []

        def responder(request):
            calls.append(1)
            return silent(request)

        bot, store, clock, _transport, _handle = build(tmp_path, responder, db=db)
        await bot.start()
        try:
            assert bot.recovered_inbound == 1
            clock.advance(10)
            await bot.autonomous_step(force=False)
            assert calls, "a recent message was dropped by the age limit"
            assert store.pending_observations(fixtures.TEST_CHAT, fixtures.TOPIC_A) == []
        finally:
            await bot.stop()
            store.close()

    run(restart())


def test_only_the_stale_prefix_is_skipped(tmp_path):
    """A stale message must not drag the cursor past a fresh one behind it.

    Tested at the store level: driving this through the bot would let start-up
    consume the fresh message first, and then the assertion would be about
    that behaviour rather than about the cursor boundary this function owns.
    """
    from app.store import Store

    store = Store(tmp_path / "direct.db")
    for sequence, text in ((1, "old"), (2, "old-2"), (3, "new")):
        with store._write() as conn:
            conn.execute(
                "INSERT INTO observation_events"
                " (update_id, chat_id, topic_id, kind, received_at, payload_json)"
                " VALUES (?, ?, ?, 'message', ?, ?)",
                (sequence, fixtures.TEST_CHAT, fixtures.TOPIC_A, store._now(),
                 '{"text": "%s"}' % text))
    with store._write() as conn:
        conn.execute(
            "UPDATE observation_events SET received_at = ?"
            " WHERE payload_json LIKE '%old%'", (1_000_000_000.0 - 7200,))

    skipped = store.expire_stale_observations(older_than=3600)
    assert skipped == [(fixtures.TEST_CHAT, fixtures.TOPIC_A, 2)], skipped

    remaining = store.pending_observations(fixtures.TEST_CHAT, fixtures.TOPIC_A)
    assert len(remaining) == 1, [r["payload_json"] for r in remaining]
    assert "new" in remaining[0]["payload_json"]
    store.close()
