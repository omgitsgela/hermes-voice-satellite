#!/usr/bin/env python3
"""Two tasks, one satellite queue: the watcher must not eat events the turn needs (issue #13).

The interruption watcher and the answer's playback waiter both pull from `sat.next_event`. The
watcher skipped every event that was not a wake detection, so a `played` arriving while it ran was
consumed and lost, and the waiter sat there for its full 180 second timeout before concluding that
playback had failed. Run with the bridge venv:

    .venv/bin/python scripts/test_event_replay.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import bridge.loop as loop_module  # noqa: E402
from wyoming.event import Event  # noqa: E402

# Find the bridge class rather than naming it: the module holds several classes, and guessing here
# already cost one failed run.
Loop = next(
    obj for obj in vars(loop_module).values()
    if isinstance(obj, type) and hasattr(obj, "_watch_for_interruption")
)

checks = 0
failures = []


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


class FakeSat:
    """Yields the queued events once, then nothing (as a quiet socket does)."""

    def __init__(self, events):
        self._events = list(events)
        self.eof = False

    async def next_event(self, timeout):
        await asyncio.sleep(0.01)
        if self._events:
            return self._events.pop(0)
        return None


def make_bridge(events):
    bridge = Loop.__new__(Loop)          # skip __init__: this tests two methods, not the world
    bridge.sat = FakeSat(events)
    bridge.self_trigger_count = 0
    return bridge


PLAYED = Event(type="played")


async def main() -> int:
    # The watcher runs while the turn waits for `played`
    bridge = make_bridge([PLAYED])
    state = types.SimpleNamespace(done=False, stop_playback=False)
    watcher = asyncio.create_task(bridge._watch_for_interruption(state))
    played = await bridge.wait_for_played(timeout=2.0)
    state.done = True
    await asyncio.sleep(0.05)
    watcher.cancel()
    try:
        await watcher
    except (asyncio.CancelledError, Exception):
        pass

    check("the turn still sees `played` while the watcher runs", played, True)

    # and a detection is still the watcher's own to consume
    bridge = make_bridge([Event(type="detection", data={"name": "hey_hermes"})])
    state = types.SimpleNamespace(done=True, stop_playback=False)
    await bridge._watch_for_interruption(state)
    check("no detection left behind in the buffer", len(bridge._replay_buffer()), 0)

    # the buffer is bounded, so a long task cannot grow it without limit
    bridge = make_bridge([])
    for _ in range(100):
        bridge._replay_buffer().append(PLAYED)
    check("buffer bounded", len(bridge._replay_buffer()) <= 32, True)

    print(f"event replay checks: {checks - len(failures)}/{checks} passed")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
