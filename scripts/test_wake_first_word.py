#!/usr/bin/env python3
"""The wake drain must remove the CHIME, not everything queued behind it (regression, 2026-10-30).

Reported: "new conversation" did not start a new session on the ESP32 satellite; the words were
passed to the existing conversation instead. The transcripts showed why:

    heard (0.5s speech): conversation.
    heard (0.6s speech): conversation.

The first word was gone. `_discard_queued_events()` threw away EVERY event waiting for the gateway,
and after a wake the queue holds however much the device sent while the Gateway was asleep for
WAKE_QUIET_SECONDS. The log said `discarded 93 event(s)` -- far more than the 308 ms chime. The
person's first word was queued behind the chime and dumped with it, the phrase matcher needs the
whole phrase, so the reset never fired.

The Pi satellite survived this because it mutes its own microphone across the chime, so there is
nothing to discard; the ESP32 has no mic mute, which is why the same Gateway code behaved differently
on the two devices.

What this pins:
1. A bounded drain drops only the chime's worth of audio by DURATION.
2. Everything still queued -- the person's speech -- is put back at the FRONT of the queue, in order,
   so the capture sees it.
3. The unbounded drain (follow-up path) still drops everything, unchanged.

Run with the Gateway venv:

    .venv/bin/python scripts/test_wake_first_word.py
"""
from __future__ import annotations

import asyncio
import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wyoming.audio import AudioChunk  # noqa: E402

from gateway.loop import VoiceLoop  # noqa: E402

RATE = 16000
BYTES_PER_SECOND = RATE * 2          # mono S16
CHUNK_BYTES = 640                    # 20 ms


def chunks(count: int) -> list:
    audio = b"\x00\x01" * (CHUNK_BYTES // 2)
    return [AudioChunk(rate=RATE, width=2, channels=1, audio=audio).event()
            for _ in range(count)]


class _NoSatellite:
    """Stands in for the socket: once the queue is empty there is simply nothing to read."""

    async def next_event(self, timeout):
        return None


def bare_loop(events: list) -> VoiceLoop:
    """A loop with no __init__: only the replay queue and _next are exercised here."""
    loop = VoiceLoop.__new__(VoiceLoop)
    loop._replay = collections.deque(events, maxlen=4096)
    loop.sat = _NoSatellite()
    return loop


def seconds(events: list) -> float:
    total = 0
    for event in events:
        if AudioChunk.is_type(event.type):
            total += len(AudioChunk.from_event(event).audio)
    return total / BYTES_PER_SECOND


checks = 0
failures: list[str] = []


def check(label: str, condition, detail: str = "") -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(f"{label}{(': ' + detail) if detail else ''}")


def main() -> int:
    chime_seconds = 0.20      # a shorter-than-real chime keeps the arithmetic obvious
    speech_seconds = 1.20
    chime_chunks = int(chime_seconds * 1000 / 20)
    speech_chunks = int(speech_seconds * 1000 / 20)
    total = chime_chunks + speech_chunks

    # 1) the bounded drain removes the chime and keeps the speech
    loop = bare_loop(chunks(total))
    dropped = asyncio.run(loop._discard_queued_events(0.35))
    remaining = list(loop._replay)
    check("dropped roughly the chime window, not the queue", 0.30 <= seconds(
        chunks(dropped)) <= 0.42, f"dropped {dropped} chunks = {dropped * 0.02:.2f}s")
    check("the person's speech is still queued", seconds(remaining) >= 1.0,
          f"remaining {seconds(remaining):.2f}s")
    check("and it is still in ORDER", len(remaining) == total - dropped,
          f"{len(remaining)} of {total - dropped}")

    # 2) order matters: the kept events must be the TAIL of the stream, not a jumble
    loop = bare_loop(chunks(total))
    asyncio.run(loop._discard_queued_events(0.35))
    check("every kept event is an audio chunk", all(AudioChunk.is_type(e.type) for e in loop._replay),
          "non-audio events must survive too")

    # 3) the unbounded drain is unchanged: the follow-up path still clears everything
    loop = bare_loop(chunks(total))
    dropped = asyncio.run(loop._discard_queued_events())
    check("an unbounded drain still empties the queue", len(list(loop._replay)) == 0)
    check("and reports every event as dropped", dropped, total)

    print(f"wake first-word checks: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


raise SystemExit(main())
