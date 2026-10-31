#!/usr/bin/env python3
"""A timer expiry is never lost because the announcement queue was full.

Regression for the case where `fire_due` removed a timer from memory and disk before invoking its
expiry callback, and the callback swallowed a full queue. The timer then vanished: it never
announced, nothing retried it, and a restart did not bring it back.

What is pinned:

  1  a refused expiry is announced later, not deleted
  2  after a successful announcement the timer is gone, from memory and from disk
  3  a queue that never drains cannot pin an expiry forever (bounded retries, then a loud drop)
  4  an undelivered expiry survives a restart
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from satellite_gateway.timers import TimerManager  # noqa: E402


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Refusing:
    """An expiry callback that refuses a number of times, like a full announcement queue."""

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.seen: list[str] = []

    async def __call__(self, timer: dict):
        self.seen.append(timer["id"])
        if self.refusals > 0:
            self.refusals -= 1
            return False
        return True


def store_at(tmp: str) -> Path:
    return Path(tmp) / "timers.json"


class ExpiryDelivery(unittest.TestCase):
    def test_a_refused_expiry_is_announced_later_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            callback = Refusing(refusals=1)
            manager = TimerManager(state_path=store_at(tmp), on_expire=callback, now=clock)
            manager.set_timer(30, "tea")
            clock.advance(31)

            announced = asyncio.run(manager.fire_due())
            self.assertEqual(announced, 0, "a refused expiry must not count as announced")
            self.assertEqual(len(manager.list_timers()), 1, "the timer was deleted instead of kept")

            clock.advance(1)
            announced = asyncio.run(manager.fire_due())
            self.assertEqual(announced, 1)
            self.assertEqual(manager.list_timers(), [], "an announced timer must not stay due")
            saved = json.loads(store_at(tmp).read_text())
            self.assertEqual(saved.get("timers"), [], "the removal must reach the file too")

    def test_a_queue_that_never_drains_gives_up_instead_of_pinning_the_timer(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            callback = Refusing(refusals=10_000)
            manager = TimerManager(state_path=store_at(tmp), on_expire=callback, now=clock)
            manager.set_timer(30, "tea")
            clock.advance(31)

            for _ in range(TimerManager.MAX_DELIVERY_RETRIES + 2):
                asyncio.run(manager.fire_due())
                clock.advance(1)

            self.assertEqual(manager.list_timers(), [],
                             "a full queue pinned the expiry forever")
            # The first attempt plus MAX_DELIVERY_RETRIES retries; the attempt after that discovers
            # the budget is spent and drops the expiry.
            self.assertEqual(len(callback.seen), TimerManager.MAX_DELIVERY_RETRIES + 1,
                             "the retry budget is not bounded")

    def test_an_undelivered_expiry_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            manager = TimerManager(state_path=store_at(tmp), on_expire=Refusing(refusals=1), now=clock)
            manager.set_timer(30, "tea")
            clock.advance(31)
            asyncio.run(manager.fire_due())

            # A new process reads the same state: the expiry is still owed.
            callback = Refusing(refusals=0)
            restarted = TimerManager(state_path=store_at(tmp), on_expire=callback, now=clock)
            announced = asyncio.run(restarted.fire_due())
            self.assertEqual(announced, 1, "the restart lost an expiry that was never announced")
            self.assertEqual(len(callback.seen), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
