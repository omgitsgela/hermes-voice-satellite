#!/usr/bin/env python3
"""Prove that a TTS stream releases the playback lock. Silent, takes a second.

This exists because of a real failure on 2026-09-22. `SatelliteClient` was imported inside
`TtsStream.__aenter__` but referenced in `__aexit__`, where that name does not exist, so the release
raised `NameError` inside its own `finally`. The lock was never freed, the next answer blocked
forever acquiring it, and from the person's side the device simply looked stuck: it woke, listened,
transcribed and never replied, with nothing in the log saying why. The same trap caught a later edit
that used `asyncio.wait_for` in a module that imported `asyncio` only inside a method.

Run this after touching playback, `TtsStream`, or the lock. It uses a fake send, so it makes no
sound, and it exits non-zero on failure so a restart can be gated on it.

    .venv/bin/python scripts/check_playback_lock.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from satellite_gateway.satellite import SatelliteClient  # noqa: E402
from satellite_gateway.tts import TtsStream  # noqa: E402


async def main() -> int:
    sent: list[str] = []

    async def fake_send(event) -> None:
        sent.append(event.type)

    stream = TtsStream(fake_send)
    async with stream:
        await stream.say("playback lock check")

    print(f"  events sent   : {sent[0]} ... {sent[-1]} ({len(sent)} total)")
    held = SatelliteClient.PLAYBACK_LOCK.locked()
    print(f"  lock held now : {held} (want False)")
    if held:
        print("  FAIL: the playback lock leaked; every later answer would block forever")
        return 1

    async with SatelliteClient.PLAYBACK_LOCK:
        print("  re-acquirable : True")
    print("  PASS: a later answer can speak")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
