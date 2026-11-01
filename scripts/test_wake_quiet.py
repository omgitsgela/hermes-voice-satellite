#!/usr/bin/env python3
"""A wake activation must ignore the opening moment, so the awake chime is not heard as speech.

Measured 2026-09-24: the device picked up its own chime, the endpointer accepted 0.3 seconds of it,
speech-to-text produced "beep", and a turn was sent on it (issue #47). Run with the Gateway venv:

    .venv/bin/python scripts/test_wake_quiet.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import satellite_gateway.loop as loop_module

Bridge = next(obj for obj in vars(loop_module).values()
              if isinstance(obj, type) and hasattr(obj, "_watch_for_interruption"))

checks = 0
failures = []


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(label + ": got " + repr(got) + ", wanted " + repr(want))


async def main():
    global checks
    gateway = Bridge.__new__(Bridge)
    # capture_after_wake scopes the no-speech budget on the endpointer, so the stub needs one.
    from satellite_gateway.endpointing import Endpointer
    gateway._ep = Endpointer(silence_ms=700, min_speech_ms=250)
    order = []

    async def discard(*_args, **_kwargs):
        order.append("discarded")
        return 2

    async def capture(wake):
        order.append("listening")
        return None

    gateway._discard_queued_events = discard
    gateway.capture_utterance = capture
    result, note = await gateway.capture_after_wake("hey_hermes")

    check("the opening moment was dropped first", order[:1], ["discarded"])
    check("listening happened after", order[1:2], ["listening"])
    check("no utterance, and it says why", result, None)
    check("the note says what happened, not a guess", note, "capture returned nothing")

    print("wake quiet checks: " + str(checks - len(failures)) + "/" + str(checks) + " passed")
    for f in failures:
        print("  FAIL: " + f)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
