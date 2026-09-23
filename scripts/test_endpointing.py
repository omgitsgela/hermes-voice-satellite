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
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.endpointing import Endpointer  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
CAPTURES = [
    ("before tones (control)", Path.home() / ".hermes/cache/audio/satellite-mic-capture.wav"),
    ("mute 908 ms, clipped", Path.home() / ".hermes/cache/audio/satellite-mic-capture-2.wav"),
    ("mute 408 ms, fixed", PROJECT / "audio/mic-20260922-060958.wav"),
]
CHUNK_SAMPLES = 1024       # what the satellite sends per audio-chunk
FRAME_MS = 20
ONSET_TOLERANCE_S = 0.15   # a few VAD frames


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


def main() -> int:
    print("=== ENDPOINTER VALIDATION (silence_ms=700, pre_roll_ms=300, webrtcvad=2) ===")
    problems = 0
    for label, path in CAPTURES:
        if not path.is_file():
            print(f"\n{label}: MISSING {path}")
            problems += 1
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

    print(f"\n=== {'ALL CHECKS PASSED' if problems == 0 else f'{problems} PROBLEM(S)'} ===")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
