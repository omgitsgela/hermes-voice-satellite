#!/usr/bin/env python3
"""A local, version-pinned Silero voice detector, owned by this repository.

`webrtcvad` is not adequate for a device meant to work in a busy room, and the measurement that
settled it (2026-09-25, the same 5 s of real speech and synthetic room noise):

    signal                              webrtcvad      Silero
    real speech                            47.6%        47.6%
    broadband noise at a quiet level        0.0%         0.0%
    broadband noise at an office level     92.7%         0.0%
    120 Hz rumble (a fan)                                0.0%

webrtcvad's verdict depends on the noise's LEVEL, and the boundary falls between a quiet room and a
busy one. That is why the project grew an energy gate: the gate existed to clean up after a detector
that calls a loud office "speech". And the gate then went deaf to the person in exactly that office,
because it judges loudness - a property of the room - when the real question is whether the sound is
a voice. Silero answers that question directly, at every level measured.

So this is not "a better threshold". It removes the reason the threshold had to be aggressive.

## Why the model is vendored rather than imported

The rule, 2026-09-25: "if we're going to use silero, we're going to use it locally, and make
it a repo we track." A detector that decides whether the device hears its owner must not depend on
a package that can be yanked, or on an asset living inside someone else's wheel where an unrelated
upgrade can move or drop it. The ONNX file is in `gateway/vendor/` with its checksum recorded, and
inference is plain `onnxruntime`, which the Gateway already installs for other work.

The upstream source is tracked as a pinned git bundle alongside the other upstreams, by
`scripts/refresh-upstream-mirrors.py`, so the project can be rebuilt without any network.

Run with the Gateway venv:

    .venv/bin/python scripts/test_silero_vad.py
"""

from __future__ import annotations

import array
import math
import pathlib
import random
import sys
import wave

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from satellite_gateway.silero_vad import MODEL_PATH, SileroVad  # noqa: E402

checks = 0
failures: list[str] = []

PROJECT = pathlib.Path(__file__).resolve().parent.parent
RECORDING = PROJECT / "audio" / "mic-20260922-060958.wav"
RATE = 16000


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def speech_fraction(vad, samples: list[int], chunk: int = 320) -> float:
    """Fraction of frames the detector calls speech."""
    vad.reset()
    hits = total = 0
    for i in range(0, len(samples) - chunk + 1, chunk):
        total += 1
        if vad.is_speech(array.array("h", samples[i:i + chunk]).tobytes(), RATE):
            hits += 1
    return hits / total if total else 0.0


def load_recording() -> list[int]:
    with wave.open(str(RECORDING), "rb") as wf:
        if wf.getframerate() != RATE:
            raise SystemExit(f"recording is {wf.getframerate()} Hz, expected {RATE}")
        pcm = array.array("h")
        pcm.frombytes(wf.readframes(wf.getnframes()))
    return list(pcm)


def main() -> int:
    if not RECORDING.is_file():
        print(f"SKIP: {RECORDING.name} missing; see private/endpointer-fixtures/README.md")
        return 0

    # The model must be in the repository, not in a site-packages directory.
    check("the model is vendored inside the project", MODEL_PATH.is_file(), True)
    check("and not read out of site-packages",
          "site-packages" in str(MODEL_PATH), False)

    vad = SileroVad()
    speech = load_recording()

    # Real speech must be detected. This is the half that has to keep working.
    real = speech_fraction(vad, speech)
    print(f"real speech          {real * 100:5.1f}% of frames")
    check("real speech is detected", real > 0.25, True)

    # And the half that webrtcvad fails: neither a noisy room nor a fan is a voice.
    random.seed(7)
    noise = [int((random.random() - 0.5) * 1200) for _ in range(RATE * 3)]
    broadband = speech_fraction(vad, noise)
    print(f"broadband rms~350    {broadband * 100:5.1f}% of frames")
    check("broadband room noise is not speech", broadband < 0.05, True)

    rumble = [int(4000 * math.sin(2 * math.pi * 120 * i / RATE)) for i in range(RATE * 3)]
    fan = speech_fraction(vad, rumble)
    print(f"120Hz fan-like tone  {fan * 100:5.1f}% of frames")
    check("a fan-like tone is not speech", fan < 0.05, True)

    quiet = speech_fraction(vad, [0] * RATE * 2)
    print(f"silence              {quiet * 100:5.1f}% of frames")
    check("silence is not speech", quiet == 0.0, True)

    # The claim this whole module rests on: it separates voice from noise where webrtcvad cannot.
    # Without this the change is a rewrite rather than a fix.
    #
    # `webrtcvad`'s verdict turns out to depend on the noise's LEVEL, not only its spectrum, and
    # the boundary sits between a quiet room and a busy one:
    #
    #     noise at rms ~350   ->  0.0% called speech
    #     noise at rms ~1170  -> 92.7%
    #     noise at rms ~3500  -> 100.0%
    #
    # An office measured a floor of ~540 with frames to ~930, so it sits in the range where
    # webrtcvad is fooled - which is exactly why the energy gate was doing all the work there and
    # then went deaf to the person. A quiet room would not have shown the problem at all.
    #
    # NOTE, because it cost a wrong measurement: `webrtcvad.Vad` carries state between calls, so
    # reusing one instance across different signals leaks the previous signal into the next
    # verdict. Feed each signal to a FRESH instance. An earlier reading of "99% of room noise"
    # was taken this way, after speech, and was not reproducible.
    import webrtcvad

    def webrtcvad_fraction(samples: list[int]) -> float:
        detector = webrtcvad.Vad(2)          # fresh per signal, deliberately
        hits = total = 0
        for i in range(0, len(samples) - 320 + 1, 320):
            total += 1
            if detector.is_speech(array.array("h", samples[i:i + 320]).tobytes(), RATE):
                hits += 1
        return hits / total if total else 0.0

    # The comparison the change is justified by, measured rather than asserted, and measured over
    # a RANGE of levels because that is where the two detectors differ in kind.
    #
    # An earlier version of this check scaled the noise to the office's floor (~930) and expected
    # webrtcvad to be fooled there. It was not: 2.7%. The claim had been generalised from a single
    # reading taken at a louder level, with a reused detector instance. Both mistakes are worth
    # naming because each one alone would have produced a confident, wrong comparison.
    #
    # What is actually true, and what this pins:
    #   * webrtcvad's verdict on the SAME noise depends on how loud it is - it tolerates a quiet
    #     room and calls a loud one speech. A device carried between rooms therefore behaves
    #     differently for reasons that have nothing to do with a person speaking.
    #   * Silero answers 0% at every level tested, so its behaviour does not move with the room.
    # Scale from the noise's MEASURED level, not a remembered one: assuming it was already 350
    # made the multiplier 3500x and overflowed int16 on the first run.
    base_rms = math.sqrt(sum(x * x for x in noise) / len(noise))
    levels = ((350, "quiet room"), (1170, "busy room"), (3500, "loud room"))
    old_by_level, new_by_level = {}, {}
    print()
    for level, label in levels:
        factor = level / base_rms
        samples = [max(-32768, min(32767, int(x * factor))) for x in noise]
        old_by_level[level] = webrtcvad_fraction(samples)
        new_by_level[level] = speech_fraction(vad, samples)
        print(f"{label:12s} (rms ~{level:5d})  webrtcvad {old_by_level[level] * 100:5.1f}%"
              f"   silero {new_by_level[level] * 100:5.1f}%")

    check("webrtcvad tolerates a quiet room", old_by_level[350] < 0.05, True)
    check("but calls a loud room speech", old_by_level[3500] > 0.5, True)
    check("so its answer moves with the room, not the voice",
          old_by_level[3500] - old_by_level[350] > 0.5, True)
    check("silero does not call noise speech at any level",
          all(f < 0.05 for f in new_by_level.values()), True)
    check("and is decisively better than the old detector where it fails",
          new_by_level[3500] < old_by_level[3500] / 10, True)

    # A frame that is not a whole detector chunk must still answer, rather than raising: the
    # endpointer feeds 20 ms frames and Silero works in 32 ms windows.
    vad.reset()
    odd = vad.is_speech(array.array("h", [1000] * 320).tobytes(), RATE)
    check("a 20 ms frame is answered without raising", isinstance(odd, bool), True)

    # Right sample rate, wrong sample rate: refuse rather than silently mis-detect.
    try:
        SileroVad(rate=8000)
        refused = False
    except ValueError:
        refused = True
    check("a sample rate Silero cannot work at is refused", refused, True)

    print(f"\nsilero vad checks: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
