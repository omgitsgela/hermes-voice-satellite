#!/usr/bin/env python3
"""Playback-lock tests for gateway.tts.TtsStream (issue #14).

`__aexit__` is not called when `__aenter__` raises, so if the opening audio-start cannot be sent
the process-wide PLAYBACK_LOCK must be released by the entry path itself, or it is held for the
life of the process and every later answer blocks. Run with the Gateway venv:

    .venv/bin/python scripts/test_tts_lock.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gateway.satellite import SatelliteClient  # noqa: E402
from gateway.tts import TtsStream  # noqa: E402

checks = 0
failures = []


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def make_send(fail_on_start: bool):
    sent = []

    async def send(event):
        sent.append(event.type)
        if fail_on_start and event.type.startswith("audio-start"):
            raise RuntimeError("satellite connection gone")
    return send, sent


async def main() -> int:
    # 1. a failed audio-start must not keep the lock
    SatelliteClient.PLAYBACK_LOCK = asyncio.Lock()
    send, sent = make_send(fail_on_start=True)
    raised = None
    try:
        async with TtsStream(send, pace=0.0):
            pass
    except RuntimeError as exc:
        raised = str(exc)
    check("entry failure propagates", raised, "satellite connection gone")
    check("lock released after failed entry", SatelliteClient.PLAYBACK_LOCK.locked(), False)
    check("only audio-start was attempted", sent, ["audio-start"])

    # 2. and the lock is genuinely reusable afterwards, which is the thing that was broken
    send2, _ = make_send(fail_on_start=False)
    async with TtsStream(send2, pace=0.0):
        check("lock held during a stream", SatelliteClient.PLAYBACK_LOCK.locked(), True)
    check("lock released after a normal stream", SatelliteClient.PLAYBACK_LOCK.locked(), False)
    check("stream closed with audio-stop", _[-1], "audio-stop")

    # 3. a cancellation mid-entry must release it too
    SatelliteClient.PLAYBACK_LOCK = asyncio.Lock()

    async def cancelled(event):
        raise asyncio.CancelledError()

    try:
        async with TtsStream(cancelled, pace=0.0):
            pass
    except asyncio.CancelledError:
        pass
    check("lock released after cancellation", SatelliteClient.PLAYBACK_LOCK.locked(), False)

    print(f"tts lock checks: {checks - len(failures)}/{checks} passed")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
