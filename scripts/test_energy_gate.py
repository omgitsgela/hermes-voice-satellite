#!/usr/bin/env python3
"""The energy gate: a frame must be loud enough to be someone talking (issue #47).

Measured on the device, 2026-09-24, 20 ms frames, this room, this preamp:

    fan baseline        median RMS  191   max   422
    YouTube at normal volume
                        median RMS  197   max   871
    the maintainer talking
                        median RMS 1026   max  9343   (speech frames only: median 2994)

Everything below is a restatement of those numbers as an assertion: 190 is what the room does, 3000
is what a person does, and a frame at 190 must never be treated as speech no matter what the voice
activity detector thinks of it. That matters because webrtcvad decides on spectral content, not
energy, and a fan's blips are spectrally speech-like - twelve of them across one day, 0.08 s each.

Run with the bridge venv:

    .venv/bin/python scripts/test_energy_gate.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import bridge.config as config_module  # noqa: E402
from bridge.endpointing import Endpointer, EnergyGate  # noqa: E402

checks = 0
failures: list[str] = []

FAN_RMS = 190          # measured fan baseline
VIDEO_RMS = 870        # the loudest single frame of a YouTube video at normal volume
SPEECH_RMS = 3000      # measured speech frames


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def frame_at(rms: int, samples: int = 320) -> bytes:
    """A 20 ms frame whose RMS is exactly `rms` (alternating +rms/-rms, so RMS == amplitude)."""
    import struct

    return struct.pack("<hh", rms, -rms) * (samples // 2)


class ScriptedVad:
    """Says whatever the script says. `decisions` is consumed one frame at a time."""

    def __init__(self, decisions: list[bool]) -> None:
        self.decisions = list(decisions)
        self.i = 0

    def is_speech(self, frame: bytes, rate: int) -> bool:
        value = self.decisions[self.i] if self.i < len(self.decisions) else False
        self.i += 1
        return value


def build(decisions: list[bool]) -> Endpointer:
    ep = Endpointer(silence_ms=700, min_speech_ms=250)
    ep._vad = ScriptedVad(decisions)
    return ep


def feed(ep: Endpointer, rms: int, frames: int):
    """Feed `frames` frames at the given level; return the first result, if any."""
    for _ in range(frames):
        result = ep.process(frame_at(rms))
        if result is not None:
            return result
    return None


def main() -> int:
    # A "fan blip": four frames (0.08 s) that webrtcvad wrongly calls speech, followed by quiet.
    # Twelve of these were logged in one day and every one was rejected only by luck of duration.
    ep = build([True] * 4 + [False] * 100)
    result = feed(ep, FAN_RMS, 8)
    check("a fan-level blip does not even start an utterance", result, None)
    check("the gate remembers the room floor from it", round(ep.gate.floor / 10) * 10, 190)

    # A fan-level blip that lasts long enough to close an utterance if it were counted as speech.
    # This is the check that proves the endpointer *consults* the gate rather than merely owning one:
    # with the gate wired in, 0.4 s of fan-level "speech" never becomes an utterance; with the gate
    # removed from the decision, it does, and is returned as an accepted utterance.
    ep = build([True] * 20 + [False] * 50)
    first = feed(ep, FAN_RMS, 20)
    result = first if first is not None else feed(ep, FAN_RMS, 50)
    check("0.4s of fan-level noise never becomes an utterance",
          None if result is None else f"an utterance of {result.speech_seconds:.2f}s", None)

    # The same shape at speech level must produce one, or the check above would pass for the wrong
    # reason (an endpointer that returns nothing at all).
    ep = build([True] * 20 + [False] * 50)
    first = feed(ep, SPEECH_RMS, 20)
    result = first if first is not None else feed(ep, FAN_RMS, 50)
    check("the same shape at speech level does become an utterance",
          result is not None and not result.rejected, True)

    # The same shape as a real utterance: a burst of loud frames, then the room again as it ends.
    # 20 speech frames at 20 ms = 0.4 s, above the 0.25 s minimum, closed by the trailing silence.
    ep = build([True] * 20 + [False] * 60)
    first = feed(ep, SPEECH_RMS, 20)
    result = first if first is not None else feed(ep, FAN_RMS, 60)
    check("real speech is not gated out", result is not None and not result.rejected, True)
    if result is not None:
        check("and its speech is counted, not discarded", round(result.speech_seconds, 2), 0.4)

    # The threshold must sit above the room and below a person, with headroom stated against BOTH
    # floors this room produces: the 190 default it starts from, and the 142 it learns from real fan
    # audio. At ratio 8 those are 1520 and 1136, so speech clears them by 2.0x and 2.6x. The
    # functional checks above already prove a real utterance still registers; this pins the headroom so
    # raising the ratio further cannot quietly deafen the device.
    ratio = config_module.VOICE_ENERGY_GATE["ratio"]
    ep = build([False] * 200)
    feed(ep, FAN_RMS, 100)
    threshold = ep.gate.threshold
    # Retuned 2026-10-29. These used to assert the threshold cleared the fan's PEAKS (422) and the
    # loudest video frame (871). On the ESP32 satellite the person's speech measures median 317 --
    # BELOW both -- and no device-side gain exists to raise it (AUDIO_MGR_MIC_GAIN and the PP AGC
    # are both acknowledged and inert, measured). Insisting on clearing the peaks therefore meant
    # rejecting the person, which is the failure mode this file exists to prevent.
    #
    # So the gate now clears the fan's MEDIAN, and peak rejection is duration's job: one leaked
    # frame is 0.02 s against a 0.25 s minimum. That trade is asserted at the video case below.
    check("threshold is above the fan's median", threshold > 191, True)
    check("speech clears the threshold at the floor it learns",
          SPEECH_RMS / (142 * ratio) >= 2.0, True)
    check("speech clears the threshold at the default floor",
          SPEECH_RMS / (190 * ratio) >= 1.9, True)

    # A long stretch of continuous speech must not drag the floor up to speech level, or the gate
    # would deafen itself after ten seconds of talking.
    ep = build([True] * 100 + [False] * 60)
    for _ in range(12):
        feed(ep, SPEECH_RMS, 100)
        feed(ep, FAN_RMS, 60)
    check("the floor stays at the room, not the speaker", ep.gate.floor < 600, True)
    check("and speech still passes after that", ep.gate.passes(frame_at(SPEECH_RMS)), True)

    # A silent room must not drive the floor to zero and the threshold with it.
    ep = build([False] * 100)
    feed(ep, 0, 100)
    check("a dead-silent room leaves a sane threshold", ep.gate.threshold >= 200, True)

    # ---------------------------------------------------------------- a busy office
    # Measured from three samples on 2026-09-25, a computer centre with a fan and other
    # people: the room's non-speech floor is ~540 (against 142-190 at home) and its loud frames
    # reach ~930, while the person's speech sits at ~1040. The room and the voice are only ~11% apart.
    #
    # The old shape - the room's 20th percentile times 6 - put the threshold at ~3240, ABOVE the
    # person, and the device answered nothing. That is the failure this section pins: the gate must
    # track the room's LOUD behaviour with a small margin, not multiply its quiet stretches.
    #
    # This does NOT require the room to be silenced. The design already accepts that loud non-speech
    # leaks (see the video note below): one leaked frame is 0.02 s and the 0.25 s minimum is what
    # stops it becoming a turn. Being deaf to the person is the worse failure.
    OFFICE_ROOM = 540        # 20th percentile of the office's non-speech frames
    OFFICE_ROOM_LOUD = 930   # its loudest non-speech frames
    OFFICE_SPEECH = 1040     # the person's speech median in that room
    ep = build([False] * 400)
    feed(ep, OFFICE_ROOM, 300)
    check("a busy office does not push the threshold above the speaker",
          ep.gate.threshold < OFFICE_SPEECH, True)
    check("so the speaker is heard in that office",
          ep.gate.passes(frame_at(OFFICE_SPEECH)), True)
    check("while the room's ordinary level is still refused",
          ep.gate.passes(frame_at(OFFICE_ROOM)), False)

    # The ceiling itself, pushed past anywhere a person could be. The point is not that such a room
    # exists but that the gate fails SAFE when it does: whatever the room does, the gate never ends
    # up above a human, because being deaf to the person is the worse failure. Without the ceiling
    # this is the room that silently turns the device into furniture.
    ep = build([False] * 400)
    feed(ep, 1500, 300)
    check("even a room louder than a person cannot push the threshold above one",
          ep.gate.threshold <= 1000, True)

    # And the shipped tuning must be the one the measurements justify.
    shipped = config_module.VOICE_ENERGY_GATE
    LEARNED_FLOOR = 142     # what the gate actually learns from this room's fan audio
    check("the shipped default floor is the measured fan level",
          round(shipped["floor_default"]), 190)
    # The requirement is the fan's ordinary level, not its peaks (see the note above): the shipped
    # pair must put the threshold above the fan's 191 median and below the person's 317 median.
    check("the shipped ratio clears the fan's median",
          shipped["ratio"] * LEARNED_FLOOR > 191, True)
    check("and the floor clamp still leaves the person above the gate",
          317 / max(shipped["threshold_min"], shipped["ratio"] * 60.0) > 1.0, True)
    # A video is explicitly NOT the gate's job: its loudest frame (871) sits just over that threshold,
    # and a louder video would put more frames over it. What has to hold is that a leaked frame cannot
    # become a turn - that is the 0.25 s minimum doing the work, so one frame is at worst a blip.
    ep = build([True] + [False] * 60)
    first = feed(ep, VIDEO_RMS, 1)
    leaked = first if first is not None else feed(ep, FAN_RMS, 60)
    check("a single video-level frame never becomes an accepted utterance",
          leaked is None or leaked.rejected, True)
    # Speech keeps the headroom that matters, against both floors this room produces. This is the
    # number to defend if anyone is tempted to raise the ratio again.
    check("speech clears the threshold at the floor it learns by 3x",
          SPEECH_RMS / (LEARNED_FLOOR * shipped["ratio"]) >= 3.0, True)
    check("speech clears the threshold at the default floor by 2.5x",
          SPEECH_RMS / (shipped["floor_default"] * shipped["ratio"]) >= 2.5, True)

    # ------------------------------------------------------------------ warmup
    # Regression, found on a live device on 2026-10-29. The gate starts from `floor_default`
    # because it has not measured this room yet. With the shipped numbers that default times the
    # ratio (190 * 6 = 1140) lands ABOVE `threshold_max`, so the ceiling pinned the gate to 1000 --
    # its loudest possible setting -- for the first `min_samples` frames of every stream (0.8 s).
    # That is exactly the window in which someone says "hello": the log recorded speech at median
    # RMS 628 rejected 35 frames out of 37 and the turn abandoned as "nothing said after the wake
    # word". A default is not a measurement and must not be able to raise the gate.
    cold = EnergyGate(**shipped)
    for _ in range(shipped["min_samples"] - 5):
        cold.observe(frame_at(200), detector_says_speech=False)
    check("an unmeasured floor cannot push the gate to its ceiling",
          cold.threshold, shipped["threshold_min"])
    check("the gate reports that the floor is not yet measured",
          cold.stats()["floor_measured"], False)
    check("speech at 628 RMS passes before the floor is measured",
          cold.passes_rms(628.0), True)

    # Once the room IS measured, a noisy room still raises the gate toward the ceiling: the fix must
    # bound the warmup, not disarm the gate.
    warm = EnergyGate(**shipped)
    for _ in range(shipped["min_samples"] + 20):
        warm.observe(frame_at(540), detector_says_speech=False)
    check("a measured noisy room still raises the gate", warm.threshold > shipped["threshold_min"], True)
    check("a measured room reports its floor as measured", warm.stats()["floor_measured"], True)
    check("the ceiling still bounds a measured room", warm.threshold <= shipped["threshold_max"], True)

    print(f"energy gate checks: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


raise SystemExit(main())
