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

from gateway.endpointing import Endpointer  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent


class ScriptedVad:
    """A detector with a known mind: it calls exactly the frames in `speech` speech. The
    boundary-preservation test below needs speech to fall on precise 20 ms frames, and the
    real Silero model's opinions are not that addressable."""

    def __init__(self, speech=()):
        self.speech = set(speech)
        self.index = 0

    def is_speech(self, frame, rate):
        decision = self.index in self.speech
        self.index += 1
        return decision

    def reset(self):
        pass


def make_endpoint(speech):
    ep = Endpointer(rate=16000, frame_ms=20, vad_aggressiveness=2, silence_ms=60,
                    min_speech_ms=60, max_utterance_seconds=10.0, pre_roll_ms=0,
                    tail_ms=40, no_speech_timeout_seconds=5.0)
    ep._vad = ScriptedVad(speech)
    # The test drives the detector, not the level gate. All the frames are silence (zero RMS),
    # so the gate must be forced to pass: threshold_min = threshold_max = -1 makes
    # passes_rms(0.0) True for every frame, and only the scripted detector decides.
    ep.gate.threshold_min = -1.0
    ep.gate.threshold_max = -1.0
    return ep


def frames_to_pcm(n_frames: int, rate: int = 16000, frame_ms: int = 20) -> bytes:
    """n_frames of 16-bit silence, long enough for the endpointer to slice into exactly that
    many frames."""
    n_samples = n_frames * rate * frame_ms // 1000
    return (np.zeros(n_samples, dtype="<i2")).tobytes()


class FeedBoundaryTests(TestCase):
    """An utterance that completes inside a feed must not destroy the feed's remaining audio.

    The Gateway feeds the endpointer chunked audio (the satellite's AudioChunk size, 1024
    samples). When an utterance ends mid-feed, `process` returns that one result and stops --
    and `_finish` -> `reset()` replaces the internal buffer. If the audio still held in that
    buffer when the boundary frame is processed is discarded, the next utterance's onset -- the
    very class of clipping the pre-roll buffer exists to prevent -- is lost: the next call sees
    a shorter stream, its frames are misaligned with the detector, and a whole utterance can
    vanish. The tail is this stream's audio, and it must survive the state reset.

    Frame numbers below use the scripted detector: speech frames 0..12, silence_ms=60 (three
    trailing frames end the utterance at frame 16), tail_ms=40 (two frames kept).
    """

    def setUp(self):
        self.frame_bytes = 16000 * 20 // 1000 * 2  # 320 samples * 2 bytes = 640

    def test_a_partial_frame_held_at_the_boundary_survives_the_reset(self):
        # The first feed ends 300 bytes into frame 19, after the utterance has already ended at
        # frame 15: when the boundary frame is processed, the buffer still holds frames 16-18
        # plus 300 of frame 19's 640 bytes. Before the fix, _finish -> reset() threw that 2220
        # byte tail away: the second feed then began mid-frame with only SEVEN complete frames
        # (500 bytes of frame 19 held, never completed), and the second utterance -- whose
        # trailing silence needs the preserved frames to reach its boundary -- never completed.
        # r2 came back None. With the tail preserved, the 500 bytes complete frame 18, the
        # utterance reaches its boundary, and the second call returns it.
        ep = make_endpoint(range(0, 13))
        total = frames_to_pcm(27)
        split = 19 * self.frame_bytes + 300
        r1 = ep.process(total[:split])
        self.assertIsNotNone(r1, 'the utterance completes inside the first feed')
        self.assertEqual(len(r1.audio), 15 * self.frame_bytes,
                         '13 speech + 3 trailing, 2 tail kept')
        # The preserved buffer (frames 16-18 + 300) is the START of the second feed's stream:
        # relative 0-2 are the preserved frames, so absolute frame 21-23 = relative 5-7. The
        # fresh VAD counts from the second feed's first frame, not from the stream's origin.
        ep._vad = ScriptedVad(range(5, 8))
        r2 = ep.process(total[split:])
        self.assertIsNotNone(r2, 'a lost tail leaves the second utterance unendable')
        # 1 pre-roll + 3 speech + 2 tail kept.
        self.assertEqual(len(r2.audio), 6 * self.frame_bytes,
                         'the second utterance is whole; a lost tail shortens or loses it')

    def test_a_split_exactly_on_the_boundary_keeps_the_following_feed(self):
        # The clean case: the first feed ends exactly at the boundary frame, so nothing is held.
        # Pins the happy path -- the tail-preservation must not corrupt the next feed when there
        # is no tail to preserve.
        ep = make_endpoint(range(0, 13))
        total = frames_to_pcm(22)
        split = 16 * self.frame_bytes
        r1 = ep.process(total[:split])
        self.assertIsNotNone(r1)
        self.assertEqual(len(r1.audio), 15 * self.frame_bytes)
        ep._vad = ScriptedVad(range(0, 3))
        r2 = ep.process(total[split:])
        self.assertIsNotNone(r2, 'the frames fed after the boundary must still be processed')
        # 6 frames fed: speech at relative 0-2, trailing at 3-5; 6 in, 3 trimmed, 2 tail kept.
        self.assertEqual(len(r2.audio), 5 * self.frame_bytes)

    def test_a_feed_larger_than_one_utterance_does_not_lose_the_second_onset(self):
        # Two utterances in two feeds, with a MID-FRAME split: the second onset only completes
        # if the first call preserves the audio it had already buffered past the boundary. The
        # first feed holds 100 bytes of frame 19 when its utterance ends at frame 15, so the
        # second feed's buffer starts with frames 16-18 + 100 bytes (2020 bytes). `process`
        # returns one result per call, so the second utterance is reachable on a later call --
        # and only if that 2020-byte tail survived the first call's reset. Before the fix it was
        # discarded, the second feed saw 13340 bytes (20 misaligned frames), its utterance never
        # reached its trailing-silence boundary, and r2 came back None.
        ep = make_endpoint(range(0, 13))
        total = frames_to_pcm(40)
        split = 19 * self.frame_bytes + 100  # 100 bytes of frame 19 are still in the feed
        r1 = ep.process(total[:split])
        self.assertIsNotNone(r1)
        self.assertEqual(len(r1.audio), 15 * self.frame_bytes)
        # The fresh VAD counts from the second feed's first frame. The preserved buffer is
        # frames 16-18 + 100, so absolute frame N = VAD index (N - 16). Speech at VAD index
        # 10-17 = absolute frames 26-33.
        ep._vad = ScriptedVad(range(10, 18))
        r2 = ep.process(total[split:])
        self.assertIsNotNone(r2, 'a lost tail drops the frames the second utterance needs')
        # 2020 + 13340 = 15360 bytes = 24 frames. Speech at VAD index 10-17; the trailing
        # silence reaches three at VAD index 20, ending the utterance with 12 frames in.
        # 1 pre-roll + 8 speech + 2 tail kept -> 11 frames.
        self.assertEqual(len(r2.audio), 11 * self.frame_bytes,
                         'the second utterance is whole, onset included')

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
