#!/usr/bin/env python3
"""Validate the endpointer against recordings whose speech onset can be measured.

Phase 2 left three captures. Two properties actually matter, and they are different:

1. **The onset must survive.** The returned clip must start at or before the true speech
   onset, otherwise the first word is clipped. This is the property we broke once already
   on the satellite side, so it is checked directly rather than inferred.
2. **The absolute onset should be accurate**, within a couple of 20 ms VAD frames.

`first_speech_at` is where speech sits INSIDE the clip and is bounded by pre_roll_ms, so it
must never be compared with an onset measured from the start of the file. That mistake made
an earlier version of this test report two false failures.

Truth is estimated independently of the VAD: the first 20 ms frame whose RMS exceeds 15% of
the loudest frame's RMS. A plain 5%-of-peak threshold would call capture 1's opening room
noise "speech" at 0.066 s.

    .venv/bin/python scripts/test_endpointing.py

## A missing recording is a SKIP, not a failure

Two of the three captures were lost when the Hermes audio cache they lived in was cleaned, and
a fixture that cannot be found used to be counted as a problem. That conflated two different
claims - "the endpointer is broken" and "the endpointer could not be checked" - and reported the
second as the first.

They are now separated. A scenario whose recording is absent is skipped loudly, the suite says
how many it verified and how many it could not, and it only fails when a recording it *could*
load produced a wrong result. A skip is not a pass: the summary never says "all checks passed"
when something was skipped.

The durable copies live in the mirror's `private/endpointer-fixtures/`, with a README recording
what each one demonstrates and how to regenerate the lost pair. That folder is protected from
the mirror's pruning, unlike the caches this used to read from.
"""

from __future__ import annotations

import os
import sys
import wave
from pathlib import Path
from unittest import TestCase

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.endpointing import Endpointer  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent

#: Where a fixture might be, most convenient first. The private copy is the durable one; the
#: others are working copies that a cache clean can remove, which is exactly what happened.
#: The private directory is a deployment value: it is a path on a network share, so it is named
#: by environment, never in code (issue #45).
_PRIVATE_FIXTURES = os.environ.get("VOICE_ENDPOINTER_FIXTURES", "").strip()
SEARCH_DIRS = (
    PROJECT / "audio",
    Path.home() / ".hermes/cache/audio",
    Path.home() / ".local/share/hermes-voice-satellite/endpointer-fixtures",
) + ((Path(_PRIVATE_FIXTURES),) if _PRIVATE_FIXTURES else ())

#: label, filename, what it demonstrates.
CAPTURES = (
    ("before tones (control)", "satellite-mic-capture.wav",
     "the control: onset intact before the mute change"),
    ("mute 908 ms, clipped", "satellite-mic-capture-2.wav",
     "the regression: the onset was clipped"),
    ("mute 408 ms, fixed", "mic-20260922-060958.wav",
     "the fix: pre-roll recovers the onset"),
)

CHUNK_SAMPLES = 1024       # what the satellite sends per audio-chunk
FRAME_MS = 20
ONSET_TOLERANCE_S = 0.15   # a few VAD frames


def find_fixture(name: str) -> Path | None:
    for directory in SEARCH_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def frame_rms_profile(pcm: np.ndarray, rate: int, frame_ms: int = FRAME_MS) -> np.ndarray:
    n = rate * frame_ms // 1000
    frames = [pcm[i:i + n] for i in range(0, len(pcm) - n + 1, n)]
    return np.array([float(np.sqrt(np.mean(f.astype("f8") ** 2))) for f in frames])


def truth_onset(pcm: np.ndarray, rate: int) -> float:
    """First frame whose RMS is a meaningful fraction of the loudest frame."""
    rms = frame_rms_profile(pcm, rate)
    if not rms.size or rms.max() <= 0:
        return float("nan")
    hits = np.where(rms > rms.max() * 0.15)[0]
    return float(hits[0]) * FRAME_MS / 1000 if hits.size else float("nan")


def reset_clears_the_detector() -> int:
    """A regression check that does not need a recording.

    The endpointer resets after every utterance and at every activation/capture boundary. The
    energy gate is deliberately left alone (the room's floor is the room's), but the *detector*
    must be reset too: the default Silero detector latches the last window's speech probability
    and carries an audio buffer, LSTM h/c state and 64-sample context between windows. A 20 ms
    frame is only 320 samples, under the 512-sample window, so the FIRST frame of a new utterance
    never completes a window and returns whatever the previous utterance latched. Left uncleared,
    an utterance that ended in speech misclassifies the next utterance's first (silent) frame as
    speech, and up to 32 ms of the previous utterance's tail pollutes the next onset. The
    detector's own reset() documents "called between utterances"; this proves the endpointer
    actually does that."""
    ep = Endpointer(rate=16000)
    vad = ep._vad
    # Drive the detector into the state an utterance that ended in speech would leave it in: a
    # latched speech decision, nonzero recurrent state, and buffered audio.
    vad._last_probability = 0.9
    vad._h[:, :] = 1.0
    vad._c[:, :] = 1.0
    vad._context[:, :] = 1.0
    vad._buffer.extend([7]*100)
    ep.reset()
    problems = 0
    if vad._last_probability > 0.5:
        print(f"  FAIL: detector latched probability {vad._last_probability} survived reset()")
        problems += 1
    if not (vad._h == 0).all() or not (vad._c == 0).all() or not (vad._context == 0).all():
        print("  FAIL: detector recurrent/context state survived reset()")
        problems += 1
    if len(vad._buffer) != 0:
        print(f"  FAIL: {len(vad._buffer)} buffered samples survived reset()")
        problems += 1
    if problems == 0:
        print("  OK    Endpointer.reset() clears the detector's carried state")
    return problems


def main() -> int:
    print("=== ENDPOINTER VALIDATION (silence_ms=700, pre_roll_ms=300, webrtcvad=2) ===")
    problems = 0
    skipped: list[tuple[str, str]] = []

    # The reset invariant runs unconditionally: it needs no fixture and pins the bug this file
    # used to ship.
    print("\nreset clears the detector")
    problems += reset_clears_the_detector()

    for label, filename, demonstrates in CAPTURES:
        path = find_fixture(filename)
        if path is None:
            # NOT a failure. Say so plainly, and say where it looked, so the next person does
            # not go hunting through the same caches.
            print(f"\nSKIP  {label}")
            print(f"  wanted        : {filename}  ({demonstrates})")
            print(f"  not found in  : {', '.join(str(d) for d in SEARCH_DIRS)}")
            print("  meaning       : this scenario was NOT verified. It is not a failure, and it")
            print("                  is not a pass either - see private/endpointer-fixtures/README.md")
            skipped.append((label, filename))
            continue

        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate()
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
        truth = truth_onset(pcm, rate)

        ep = Endpointer(rate=rate)
        result = None
        for i in range(0, len(pcm), CHUNK_SAMPLES):
            result = ep.process(pcm[i:i + CHUNK_SAMPLES].tobytes())
            if result is not None:
                break

        print(f"\n{label}  ({path.name})")
        print(f"  audio          : {len(pcm) / rate:.2f} s, truth onset {truth:.3f} s")
        if result is None:
            print("  RESULT         : endpointer produced NOTHING")
            problems += 1
            continue

        clip_start = result.speech_absolute_at - result.first_speech_at
        delta = result.speech_absolute_at - truth
        print(f"  {result}")
        print(f"  clip starts at : {clip_start:.3f} s "
              f"(truth onset {truth:.3f} s -> pre-roll recovered "
              f"{max(0.0, truth - clip_start) * 1000:.0f} ms)")
        print(f"  onset accuracy : {delta:+.3f} s "
              f"({'OK' if abs(delta) <= ONSET_TOLERANCE_S else 'FAIL'})")

        if clip_start > truth + FRAME_MS / 1000:
            print("  FAIL: the clip starts AFTER the true onset; the first word is clipped")
            problems += 1
        if abs(delta) > ONSET_TOLERANCE_S:
            problems += 1
        if abs(result.duration - len(result.audio) / (rate * 2)) > 1e-6:
            print("  FAIL: reported duration does not match the audio length")
            problems += 1

    verified = len(CAPTURES) - len(skipped)

    print()
    if problems:
        print(f"=== FAILED: {problems} PROBLEM(S) "
              f"({verified} verified, {len(skipped)} skipped) ===")
        return 1

    if skipped:
        # Deliberately NOT "all checks passed". Something was not checked, and the summary
        # must not imply otherwise - that is how a skip quietly becomes a false assurance.
        print(f"=== {verified} VERIFIED, {len(skipped)} SKIPPED (recording missing) ===")
        for label, filename in skipped:
            print(f"    not verified: {label}  ({filename})")
        print("    The endpointer was NOT fully validated by this run.")
        return 0

    print("=== ALL CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
