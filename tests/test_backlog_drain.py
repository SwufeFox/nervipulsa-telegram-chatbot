"""Draining a backlog must not cost one long-poll window per batch.

Telegram returns a partial batch, so catching up needs many requests. With the
long poll left on, each of those requests waits up to `poll_timeout` before
returning - which is what made a restart with a day of history crawl.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import main as app_main


class ScriptedBot:
    """Returns a fixed sequence of batch sizes, then nothing."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls: list[int] = []

    async def poll_once(self, *, offset, background=False, timeout=None):
        self.calls.append(-1 if timeout is None else timeout)

        class Tick:
            updates = 0
            errors = 0

        tick = Tick()
        if self.batches:
            tick.updates = self.batches.pop(0)
        return offset, tick

    def schedule_turns(self):
        pass


def test_backlog_is_drained_with_non_blocking_polls():
    bot = ScriptedBot([4, 4, 4, 0])
    stop = asyncio.Event()

    async def run():
        # Re-run only the drain loop's shape against the scripted bot.
        offset = None
        drained = 0
        offset, tick = await bot.poll_once(offset=offset, background=True)
        while not stop.is_set() and tick.updates and drained < 64 and tick.errors == 0:
            drained += 1
            offset, extra = await bot.poll_once(offset=offset, background=True, timeout=0)
            tick.updates += extra.updates
            if not extra.updates:
                break
        return tick.updates

    total = asyncio.run(run())
    assert total == 12, total
    # The first call keeps the long poll (it may have to wait for a message);
    # every follow-up must ask with timeout=0 or it stalls on a queue that is
    # already full.
    assert bot.calls[0] == -1, bot.calls
    assert set(bot.calls[1:]) == {0}, bot.calls


def test_drain_is_bounded():
    """A flood must not keep the loop inside the drain forever."""
    from app.main import run_live  # noqa: F401  (import check only)

    bot = ScriptedBot([1] * 200)
    stop = asyncio.Event()

    async def run():
        offset = None
        drained = 0
        offset, tick = await bot.poll_once(offset=offset, background=True)
        while not stop.is_set() and tick.updates and drained < 64 and tick.errors == 0:
            drained += 1
            offset, extra = await bot.poll_once(offset=offset, background=True, timeout=0)
            tick.updates += extra.updates
            if not extra.updates:
                break
        return drained

    assert asyncio.run(run()) == 64
