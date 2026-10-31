#!/usr/bin/env python3
"""Transient expiry storage failures must not kill scheduling or lose timers (#84)."""
import asyncio
import contextlib
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from gateway.timers import TimerManager


class TimerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_retries_failed_expiry_without_losing_timers(self):
        for fail_at_start in (False, True):
            with self.subTest(fail_at_start=fail_at_start):
                clock = [100.0]
                fired = []
                failed = asyncio.Event()
                delivered = asyncio.Event()
                unavailable = False

                def expire(timer):
                    fired.append(timer["id"])
                    delivered.set()

                def save(*_args):
                    if unavailable:
                        failed.set()
                        raise OSError("simulated storage outage")

                with patch.object(TimerManager, "_load", return_value=[]), \
                        patch.object(TimerManager, "_save", side_effect=save):
                    manager = TimerManager(on_expire=expire, now=lambda: clock[0])
                    first = manager.set_timer(1, "first")
                    try:
                        if not fail_at_start:
                            await manager.start()
                        clock[0] += 2
                        unavailable = True
                        if fail_at_start:
                            await manager.start()
                        else:
                            manager._signal_change()
                        await asyncio.wait_for(failed.wait(), 1)
                        await asyncio.sleep(0)
                        self.assertFalse(manager._task.done(), "scheduler died after save error")
                        self.assertEqual([t["id"] for t in manager.list_timers()], [first["id"]])
                        self.assertEqual(fired, [], "expiry delivered before persistence succeeded")
                        task = manager._task
                        await manager.start()
                        self.assertIs(manager._task, task, "start duplicated a healthy scheduler")

                        unavailable = False
                        # No new timer or change signal: recovery must retry by itself.
                        await asyncio.wait_for(delivered.wait(), 2.5)
                        self.assertEqual(fired, [first["id"]])
                        delivered.clear()
                        second = manager.set_timer(1, "second")
                        clock[0] += 2
                        manager._signal_change()
                        await asyncio.wait_for(delivered.wait(), 1)
                        self.assertEqual(fired, [first["id"], second["id"]])
                        self.assertEqual(manager.list_timers(), [])
                        self.assertFalse(manager._task.done())
                    finally:
                        if manager._task is not None:
                            manager._task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, OSError):
                                await manager._task


if __name__ == "__main__":
    unittest.main()
