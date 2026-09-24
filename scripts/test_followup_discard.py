#!/usr/bin/env python3
"""The follow-up quiet interval must discard stale audio, not merely delay it (issue #40).

The device's done tone arrives as ordinary audio chunks. Sleeping through the quiet interval only
delays reading them, so the endpointer hears our own chime as speech and can close the window on a
word nobody said. Run with the bridge venv:

    .venv/bin/python scripts/test_followup_discard.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import bridge.loop as loop_module  # noqa: E402
from wyoming.audio import AudioChunk  # noqa: E402

Loop = next(obj for obj in vars(loop_module).values()
            if isinstance(obj, type) and hasattr(obj, "_watch_for_interruption"))

checks = 0
failures = []


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def chunk_event():
    return AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00" * 320).event()


class Sat:
    def __init__(self, events):
        self._events = list(events)
        self.eof = False

    async def next_event(self, timeout):
        return self._events.pop(0) if self._events else None


async def main() -> int:
    # 1. queued chime samples are dropped, and counted
    b = Loop.__new__(Loop)
    b.sat = Sat([chunk_event(), chunk_event(), chunk_event()])
    dropped = await b._discard_queued_events()
    check("three stale chunks discarded", dropped, 3)
    check("queue is empty afterwards", await b._next(0), None)

    # 2. an empty queue is not an error and drops nothing
    b = Loop.__new__(Loop)
    b.sat = Sat([])
    check("nothing to discard", await b._discard_queued_events(), 0)

    # 3. anything buffered from a task is cleared too, so it cannot reappear here
    b = Loop.__new__(Loop)
    b.sat = Sat([chunk_event()])
    b._replay_buffer().append("stale")
    check("buffer cleared as well", await b._discard_queued_events(), 2)

    # 4. the quiet interval actually calls it, since that is the whole point
    src = (pathlib.Path(__file__).resolve().parent.parent / "bridge" / "loop.py").read_text()
    quiet = src.index("await asyncio.sleep(FOLLOW_UP_QUIET_SECONDS)")
    check("discard happens after the quiet sleep",
          "_discard_queued_events()" in src[quiet:quiet + 500], True)

    print(f"follow-up discard checks: {checks - len(failures)}/{checks} passed")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
