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
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge.config as config_module  # noqa: E402
import bridge.loop as loop_module  # noqa: E402
from bridge.endpointing import Endpointer  # noqa: E402
from bridge.endpointing import EndpointResult  # noqa: E402
from bridge.satellite import SatelliteClient  # noqa: E402


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


def _feeder(events: list):
    """A scripted `_next`: hands out the queued events, then returns None."""
    async def nxt(timeout):
        return events.pop(0) if events else None
    return nxt


def make_loop(script: list[EndpointResult | None]):
    """A VoiceLoop with only the pieces capture_after_wake touches, plus a scripted capture."""
    loop = object.__new__(loop_module.VoiceLoop)
    loop.sat = SatelliteClient("127.0.0.1")
    # capture_after_wake now scopes the no-speech budget on the endpointer, so the stub needs one.
    from bridge.endpointing import Endpointer
    loop._ep = Endpointer(silence_ms=700, min_speech_ms=250)
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
        ok = endpoint is None and len(calls) == 0 and "only blips" in note
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

    # 5. The fan case (2026-09-24). Blips arriving every 100 ms must close the activation at the
    #    no-speech bound. Before this, each blip restarted the endpointer's own no-speech window, so
    #    an empty activation with a fan in the room ran the full 30 second cap every time.
    from bridge.config import WAKE_NO_SPEECH_SECONDS as _orig_no_speech

    config_module.WAKE_NO_SPEECH_SECONDS = 0.3
    loop_module.CAPTURE_ACTIVATION_SECONDS = 30.0
    try:
        loop = object.__new__(loop_module.VoiceLoop)
        loop.sat = SatelliteClient("127.0.0.1")
        loop._ep = Endpointer(silence_ms=700, min_speech_ms=250)
        attempts: list[int] = []

        async def blip_every_100ms(wake_word: str):
            attempts.append(1)
            await asyncio.sleep(0.1)
            return blip()

        loop.capture_utterance = blip_every_100ms  # type: ignore[method-assign]
        started = time.monotonic()
        endpoint, note = await loop.capture_after_wake("hey_hermes")
        elapsed = time.monotonic() - started
        # The quiet interval (0.5 s) plus the 0.3 s bound, nowhere near the 30 s cap.
        ok = (endpoint is None and elapsed < 3.0 and len(attempts) <= 6
              and "no words in" in note)
        print("\n5. blips every 100ms, bound 0.3s")
        print(f"   elapsed: {elapsed:.2f}s  captures: {len(attempts)}  note: {note}")
        if not ok:
            print("   FAIL: blips extended the activation past the no-speech bound")
            problems += 1
        else:
            print("   OK: closed on the no-speech bound instead of the 30s cap")
    finally:
        config_module.WAKE_NO_SPEECH_SECONDS = _orig_no_speech

    # 6. At the endpointer level, which is where the 8 second bound actually lives. A blip must not
    #    reset the no-speech budget: before this, each blip zeroed the frame count, `no_speech` could
    #    never fire, and only the wall-clock cap ended an activation. Measured live 2026-09-24: an
    #    activation with one stray blip ran 14.4s instead of 8s for exactly this reason.
    pattern = [True] * 4 + [False] * 96           # a rejected 0.08s blip every 2 s of frames
    ep = Endpointer(silence_ms=700, min_speech_ms=250)

    class PatternVad:
        def __init__(self):
            self.i = 0

        def is_speech(self, frame, rate):
            value = pattern[self.i % len(pattern)]
            self.i += 1
            return value

    ep._vad = PatternVad()
    speech_frame = b"\x11\x22" * ep.frame_samples
    quiet_frame = b"\x00\x00" * ep.frame_samples
    result = None
    frames = 0
    for n in range(1, 3000):
        frames = n
        result = ep.process(speech_frame if pattern[(n - 1) % len(pattern)] else quiet_frame)
        if result is not None and result.ended_by == "no_speech":
            break
    seconds = frames * 0.02
    print("\n6. blips every 2s at the endpointer")
    print(f"   no_speech after {frames} frames ({seconds:.1f}s of audio)")
    # 8 s of frames is the endpointer's own window: 400 frames at 20 ms. The point is that the blips'
    # frames COUNT towards it rather than restarting it.
    if result is None or result.ended_by != "no_speech" or not (380 <= frames <= 430):
        print(f"   FAIL: expected no_speech at about 400 frames, got "
              f"{'nothing' if result is None else result.ended_by} at {frames}")
        problems += 1
    else:
        print("   OK: the blips accumulated towards the bound instead of resetting it")

    # 7. The budget's scope, through the real capture path. This is the check the live test caught
    #    and the offline one missed: `capture_utterance` called `reset()` on entry, which wiped the
    #    budget that `_finish` had just carried, so each attempt started from zero and the 8 s bound
    #    still took 14.5 s. Only visible across TWO attempts.
    from wyoming.audio import AudioChunk

    loop2 = object.__new__(loop_module.VoiceLoop)
    loop2.sat = types.SimpleNamespace(eof=False)
    loop2._ep = Endpointer(silence_ms=700, min_speech_ms=250)
    loop2.turn_started_at = None
    loop2._ep.begin_activation()
    f_samples = loop2._ep.frame_samples
    blip_frame = b"\x11\x22" * f_samples
    quiet_frame = b"\x00\x00" * f_samples

    class FramePlan:
        """Feeds a planned run of frames, marking a 0.08 s blip at the end of the first burst."""

        def __init__(self, plan):
            self.plan = list(plan)

        def is_speech(self, frame, rate):
            return bool(self.plan.pop(0)) if self.plan else False

    def event_for(decision):
        return AudioChunk(audio=blip_frame if decision else quiet_frame,
                          rate=16000, width=2, channels=1).event()

    # Attempt 1: 255 quiet frames, then a 4-frame blip, then the silence that closes it as rejected.
    first = ([False] * 255 + [True] * 4 + [False] * 40)
    loop2._ep._vad = FramePlan(first)
    queue = [event_for(d) for d in first]
    loop2._next = _feeder(queue)
    attempt1 = await loop2.capture_utterance("hey_hermes")
    carried = loop2._ep._frames_seen

    # Attempt 2: quiet only. If the budget survived, 8 s of accumulated frames is already nearly spent
    # and `no_speech` arrives within ~100 frames. If it was cleared on entry, this finds nothing.
    second = [False] * 140
    loop2._ep._vad = FramePlan(second)
    loop2._next = _feeder([event_for(d) for d in second])
    attempt2 = await loop2.capture_utterance("hey_hermes")

    print("\n7. the no-speech budget across two capture attempts")
    print(f"   attempt 1: {'rejected blip' if attempt1 and attempt1.rejected else attempt1}"
          f", {carried} frames carried")
    print(f"   attempt 2: {attempt2.ended_by if attempt2 else 'nothing'}")
    if not (attempt1 is not None and attempt1.rejected and carried > 200):
        print("   FAIL: the first attempt did not end as a rejected blip with a carried budget")
        problems += 1
    elif attempt2 is None or attempt2.ended_by != "no_speech":
        print("   FAIL: the budget did not survive the second attempt's entry - the 8 s bound will")
        print("         still be defeated by a blip, as the live test showed")
        problems += 1
    else:
        print("   OK: the blip's frames counted towards the bound across attempts")

    print(f"\n=== {'ALL CHECKS PASSED' if problems == 0 else f'{problems} PROBLEM(S)'} ===")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
