#!/usr/bin/env python3
"""Bounded subprocess regression for both playback-consumer orders (#82)."""
import asyncio
import contextlib
import pathlib
import subprocess
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def scenario(order):
    from bridge.loop import VoiceLoop
    from wyoming.event import Event

    class Satellite:
        eof = False

        def __init__(self):
            self.queue = asyncio.Queue()
            self.readers = []

        async def next_event(self, timeout):
            self.readers.append(asyncio.current_task().get_name())
            try:
                return await asyncio.wait_for(self.queue.get(), timeout)
            except asyncio.TimeoutError:
                return None

    bridge = VoiceLoop.__new__(VoiceLoop)
    bridge.sat = Satellite()
    bridge.self_trigger_count = 0
    state = types.SimpleNamespace(done=False, stop_playback=False)
    tasks = {}
    coroutines = {
        "watcher": lambda: bridge._watch_for_interruption(state),
        "waiter": lambda: bridge.wait_for_played(timeout=10),
    }
    try:
        for name in order.split("-"):
            tasks[name] = asyncio.create_task(coroutines[name](), name=name)
            while len(bridge.sat.queue._getters) < len(tasks):
                await asyncio.sleep(0)
        await bridge.sat.queue.put(Event(type="played"))
        assert await asyncio.wait_for(tasks["waiter"], 1.5), "Played was lost"
        assert not bridge._replay_buffer(), "Played was delivered twice"
    finally:
        state.done = True
        for task in tasks.values():
            task.cancel()
        for task in tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task


class ReplaySchedulingTests(unittest.TestCase):
    def test_played_reaches_waiter_in_both_consumer_orders(self):
        for order in ("waiter-watcher", "watcher-waiter"):
            with self.subTest(order=order):
                try:
                    result = subprocess.run(
                        [sys.executable, "-B", __file__, order], cwd=ROOT,
                        capture_output=True, text=True, timeout=5,
                    )
                except subprocess.TimeoutExpired:
                    self.fail(f"{order}: event loop starved (external timeout)")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("waiter-watcher", "watcher-waiter"):
        asyncio.run(scenario(sys.argv[1]))
    else:
        unittest.main()
