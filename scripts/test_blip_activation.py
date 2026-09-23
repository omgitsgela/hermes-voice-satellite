#!/usr/bin/env python3
"""Prove a rejected blip no longer ends the activation.

Michael's report, 2026-09-22: "the ok nabu activation is cutting off too early before he
actually speaks". The bridge journal showed exactly why:

    16:42:42  wake word: okay_nabu
    16:42:43  endpoint (silence): 0.28s audio, 0.08s speech, ended_by=silence, REJECTED
    16:42:49  wake word: okay_nabu          <- he had to say it again

The satellite's wake chime leaks into the mic as a few dozen milliseconds of "speech" at the very
start of the stream. The endpointer then needed 700 ms of silence to end the utterance, which it
got while he was still drawing breath, so it reported a blip, and the old code stopped the mic and
closed the activation. The fix: discard the noise and keep listening, bounded by
CAPTURE_ACTIVATION_SECONDS, and treat a full window of silence (`no_speech`) as an empty room.

This test drives that decision directly with scripted endpoints, so it needs no device, no mic and
no network. It is the regression guard for the behaviour, not a re-test of the endpointer (which
scripts/test_endpointing.py covers against real recordings).

    .venv/bin/python scripts/test_blip_activation.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge.loop as loop_module  # noqa: E402
from bridge.endpointing import EndpointResult  # noqa: E402


def blip(speech_seconds: float = 0.08) -> EndpointResult:
    """What the wake chime looks like to the endpointer: a few frames of noise, then silence."""
    return EndpointResult(audio=b"\0" * 8960, rate=16000, duration=0.28,
                          speech_seconds=speech_seconds, first_speech_at=0.0,
                          speech_absolute_at=0.0, ended_by="silence", rejected=True)


def real_utterance() -> EndpointResult:
    return EndpointResult(audio=b"\0" * 38400, rate=16000, duration=1.2,
                          speech_seconds=1.0, first_speech_at=0.3,
                          speech_absolute_at=0.3, ended_by="silence", rejected=False)


def silent_window() -> EndpointResult:
    """Nothing said for the whole no-speech window: an empty room, not a blip."""
    return EndpointResult(audio=b"", rate=16000, duration=0.0, speech_seconds=0.0,
                          first_speech_at=0.0, speech_absolute_at=0.0,
                          ended_by="no_speech", rejected=True)


def make_loop(script: list[EndpointResult | None]):
    """A VoiceLoop with only the pieces capture_after_wake touches, plus a scripted capture."""
    loop = object.__new__(loop_module.VoiceLoop)
    calls: list[str] = []

    async def fake_capture(wake_word: str):
        calls.append(wake_word)
        return script.pop(0) if script else real_utterance()

    loop.capture_utterance = fake_capture  # type: ignore[method-assign]
    return loop, calls


async def main() -> int:
    print("=== ACTIVATION SURVIVES A BLIP (Michael's case, 2026-09-22) ===")
    problems = 0

    # 1. The reported failure: chime blip, then he speaks. Must return the real utterance.
    loop, calls = make_loop([blip(), blip(0.10), real_utterance()])
    endpoint, note = await loop.capture_after_wake("okay_nabu")
    ok = endpoint is not None and not endpoint.rejected and len(calls) == 3
    print(f"\n1. blip, blip, then speech")
    print(f"   captures: {len(calls)}  result: {'real utterance' if ok else 'FAILED'}"
          f"  note: {note or '-'}")
    if not ok:
        print("   FAIL: the activation did not survive the blips")
        problems += 1
    else:
        print("   OK: both blips ignored, the real utterance came through")

    # 2. A full window of silence is an empty room: close it, do not keep listening.
    loop, calls = make_loop([silent_window(), real_utterance()])
    endpoint, note = await loop.capture_after_wake("okay_nabu")
    ok = endpoint is None and len(calls) == 1 and "nothing said" in note
    print("\n2. nobody there after the wake")
    print(f"   captures: {len(calls)}  result: {endpoint}  note: {note}")
    if not ok:
        print("   FAIL: silence should close the activation on the first window")
        problems += 1
    else:
        print("   OK: closed on the first silent window, no second listen")

    # 3. Endless blips must not hold the mic open forever.
    original = loop_module.CAPTURE_ACTIVATION_SECONDS
    loop_module.CAPTURE_ACTIVATION_SECONDS = 0.0
    try:
        loop, calls = make_loop([blip(), blip(), blip(), blip()])
        endpoint, note = await loop.capture_after_wake("okay_nabu")
        ok = endpoint is None and len(calls) == 1 and "only blips" in note
        print("\n3. endless blips (deadline 0 s)")
        print(f"   captures: {len(calls)}  result: {endpoint}  note: {note}")
        if not ok:
            print("   FAIL: the deadline did not bound the activation")
            problems += 1
        else:
            print("   OK: gave up at the deadline instead of listening forever")
    finally:
        loop_module.CAPTURE_ACTIVATION_SECONDS = original

    # 4. A clean activation still behaves exactly as before.
    loop, calls = make_loop([real_utterance()])
    endpoint, note = await loop.capture_after_wake("okay_nabu")
    ok = endpoint is not None and not endpoint.rejected and len(calls) == 1 and note == ""
    print("\n4. speech straight away")
    print(f"   captures: {len(calls)}  result: {'real utterance' if ok else 'FAILED'}")
    if not ok:
        print("   FAIL: a normal utterance no longer returns on the first capture")
        problems += 1
    else:
        print("   OK: unchanged from before the fix")

    print(f"\n=== {'ALL CHECKS PASSED' if problems == 0 else f'{problems} PROBLEM(S)'} ===")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
