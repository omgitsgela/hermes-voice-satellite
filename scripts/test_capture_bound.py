#!/usr/bin/env python3
"""CAPTURE_ACTIVATION_SECONDS is a wall-clock bound, not a blip count (issue #36).

capture_after_wake computed a deadline but only consulted it after a rejected blip, while each
capture_utterance call waits up to 30s for events of its own. A wake with an unending stream of
noise therefore held the microphone well past the advertised window. Run with the bridge venv:

    .venv/bin/python scripts/test_capture_bound.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import bridge.loop as loop_module  # noqa: E402

Loop = next(obj for obj in vars(loop_module).values()
            if isinstance(obj, type) and hasattr(obj, "_watch_for_interruption"))

CAP = 0.30   # patched into the module so the test does not take fifteen seconds
loop_module.CAPTURE_ACTIVATION_SECONDS = CAP

checks = 0
failures = []


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def bridge_with(capture):
    b = Loop.__new__(Loop)
    b.capture_utterance = capture
    return b


def endpoint(*, rejected=False, ended_by="", speech_seconds=1.0):
    return types.SimpleNamespace(rejected=rejected, ended_by=ended_by,
                                 speech_seconds=speech_seconds, audio=b"")


async def main() -> int:
    # 1. a capture that never finishes must still be cut off at the cap
    async def never_finishes(_wake):
        await asyncio.sleep(30.0)

    b = bridge_with(never_finishes)
    started = time.monotonic()
    result, note = await asyncio.wait_for(b.capture_after_wake("hey_hermes"), timeout=5.0)
    elapsed = time.monotonic() - started
    check("a stuck capture returns at all", result, None)
    check("  bounded by the cap", elapsed < 2.0, True)
    check("  honest note", "in 0s after the wake" in note or "after the wake" in note, True)

    # 2. an endless stream of blips is bounded too, and says so
    async def blips_forever(_wake):
        await asyncio.sleep(CAP / 3)
        return endpoint(rejected=True, speech_seconds=0.05)

    b = bridge_with(blips_forever)
    started = time.monotonic()
    result, note = await asyncio.wait_for(b.capture_after_wake("hey_hermes"), timeout=5.0)
    check("blips return None", result, None)
    check("  blips bounded by the cap", time.monotonic() - started < 2.0, True)
    check("  note mentions the window", "after the wake" in note, True)

    # 3. a real utterance still comes straight back
    async def good(_wake):
        return endpoint(rejected=False)

    b = bridge_with(good)
    result, note = await asyncio.wait_for(b.capture_after_wake("hey_hermes"), timeout=5.0)
    check("a real utterance is returned", result is not None, True)
    check("  with no note", note, "")

    print(f"capture bound checks: {checks - len(failures)}/{checks} passed")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
