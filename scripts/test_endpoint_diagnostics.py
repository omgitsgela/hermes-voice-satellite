#!/usr/bin/env python3
"""Check that a failed turn can say WHO refused the frames, not just that it failed.

A turn that hears nothing and a room with nobody in it both end in "nothing said after the wake
word". Before this they were indistinguishable from the outside, and they need opposite fixes:

* the level gate refusing frames the detector accepted  -> the gate is deaf to this speaker in
  this room; the gate or its ratio is what needs changing
* the detector never accepting the voice at all          -> no amount of gate tuning helps; that
  is a detector, aggressiveness or audio-path problem

The bridge already logged the *outcome* of endpointing. What it did not log was the reason, or
what the room looked like when the decision was made - which is why a real mis-calibration took a
day to find instead of one turn.

These checks drive the real endpointer over the one surviving real recording and then read the
LOG LINES, the same thing an operator would see with `journalctl`. Testing the log rather than the
object matters: the per-utterance counters are cleared by `_finish`, so the attributes only ever
describe the last utterance, while the log line is the artifact anyone will actually diagnose from.

    .venv/bin/python scripts/test_endpoint_diagnostics.py

Uses real speech rather than a synthetic tone, because the detector's verdict is what is being
attributed and a synthetic signal would not stand in for it.
"""

from __future__ import annotations

import array
import logging
import re
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.endpointing import Endpointer  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
RECORDING = PROJECT / "audio" / "mic-20260922-060958.wav"

CHUNK = 1024
#: The recording is 120 of 252 frames of speech; anything near that means the detector engaged.
MIN_VAD_FRAMES = 50

LEVELS_LINE = re.compile(r"^  levels: room p20=(\d+) p50=(\d+) p90=(\d+) max=(\d+) \(n=(\d+)\)"
                         r" \| floor=(\d+) thr=(\d+)"
                         r" \| frames: vad_speech=(\d+) gate_rejected=(\d+) accepted=(\d+)"
                         r" \| vad_speech_rms med=(\d+) max=(\d+)$")


class Capture(logging.Handler):
    """Collect the endpointer's own log lines, so the check tests what an operator would see."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def drain(ep: Endpointer, pcm: bytes) -> int:
    """Feed a whole recording. Returns how many endpoints completed."""
    count = 0
    for i in range(0, len(pcm), CHUNK):
        if ep.process(pcm[i:i + CHUNK]) is not None:
            count += 1
    return count


def totals(lines: list[str]) -> dict:
    """Sum one run's per-utterance lines into a single picture."""
    out = {"vad_speech": 0, "gate_rejected": 0, "accepted": 0, "thresholds": [], "uttered": 0}
    for line in lines:
        m = LEVELS_LINE.match(line)
        if not m:
            continue
        out["uttered"] += 1
        out["vad_speech"] += int(m.group(8))
        out["gate_rejected"] += int(m.group(9))
        out["accepted"] += int(m.group(10))
        out["thresholds"].append(int(m.group(7)))
    return out


def run(pcm: bytes, floor: float | None = None) -> tuple[list[str], int]:
    capture = Capture()
    log = logging.getLogger("bridge.endpointing")
    log.addHandler(capture)
    log.setLevel(logging.INFO)
    try:
        ep = Endpointer(rate=16000)
        endpoints = drain(ep, pcm)
    finally:
        log.removeHandler(capture)
    return [ln for ln in capture.lines if ln.startswith("  levels:")], endpoints


def attenuate(pcm: bytes, gain: float) -> bytes:
    """Turn the same speech down, which is what standing further away does.

    Two wrong models were tried before this one, and both are worth recording.

    Setting `gate.floor` directly does nothing: `observe()` recomputes the floor from every
    non-speech frame, so a forced value is overwritten within a frame or two. That is the gate
    working correctly - the floor is learned from the room, not assigned.

    Adding broadband room noise is also wrong, and it looked like it disproved the diagnosis:
    the noise raises the floor AND every speech frame with it, so the gate ended up refusing
    slightly FEWER frames at a threshold five times higher. What matters is not the room's level
    on its own but the RATIO between the speaker and the room.

    The gate requires the speaker to clear 6x the room's floor. Attenuation is therefore the
    honest model of the failure: the room is unchanged, the speaker simply stops being 6x it.
    """
    samples = array.array("h", pcm)
    out = array.array("h", (max(-32768, min(32767, int(v * gain))) for v in samples))
    return out.tobytes()


def main() -> int:
    problems = 0

    if not RECORDING.is_file():
        print(f"SKIP: {RECORDING.name} is not present, so the levels cannot be checked.")
        print("      It lives in the mirror's private/endpointer-fixtures/ - see its README.")
        return 0

    with wave.open(str(RECORDING), "rb") as wf:
        rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    peak = max(abs(v) for v in array.array("h", pcm))
    print(f"=== recording: {len(pcm) / (rate * 2):.2f}s @ {rate}Hz, peak {peak} ===")

    # ------------------------------------------------------------- a normal room
    normal_lines, normal_eps = run(pcm)
    normal = totals(normal_lines)
    print(f"\nnormal room: endpoints={normal_eps} uttered_lines={normal['uttered']}")
    print(f"  vad_speech={normal['vad_speech']}  gate_rejected={normal['gate_rejected']}"
          f"  accepted={normal['accepted']}  thresholds={normal['thresholds']}")

    if not normal_lines:
        print("  FAIL: no 'levels:' line was logged at all, so a failed turn still cannot be"
              " explained. That was the entire point of the change.")
        return 1
    if normal["vad_speech"] != normal["gate_rejected"] + normal["accepted"]:
        print(f"  FAIL: vad_speech ({normal['vad_speech']}) != gate_rejected"
              f" ({normal['gate_rejected']}) + accepted ({normal['accepted']}); some frame was"
              " refused without being attributed, which is the bug this line exists to prevent")
        problems += 1
    if normal["vad_speech"] < MIN_VAD_FRAMES:
        print(f"  FAIL: only {normal['vad_speech']} frames looked like speech to the detector;"
              " real speech should produce far more")
        problems += 1
    if normal["accepted"] == 0:
        print("  FAIL: real speech in a normal room was refused by BOTH stages")
        problems += 1

    # -------------------------------------------- the case that actually broke
    # Same speech, same room, same detector - only the speaker is further from the microphone,
    # which is what a device used "in all kinds of places" has to cope with. The room's floor is
    # unchanged, so this isolates the RATIO the gate actually depends on. The diagnostic has to
    # blame the GATE here, not the detector: the detector's verdict does not depend on level.
    distant = attenuate(pcm, 0.5)
    distant_lines, distant_eps = run(distant)
    far = totals(distant_lines)
    print(f"\ndistant speaker (same room, speech turned down 6dB): endpoints={distant_eps}")
    print(f"  vad_speech={far['vad_speech']}  gate_rejected={far['gate_rejected']}"
          f"  accepted={far['accepted']}  thresholds={far['thresholds']}")

    if far["vad_speech"] != far["gate_rejected"] + far["accepted"]:
        print("  FAIL: attribution broke once the gate started refusing frames")
        problems += 1
    if far["vad_speech"] < MIN_VAD_FRAMES:
        print(f"  FAIL: the detector stopped calling the speech speech ({far['vad_speech']} frames)"
              " when only the LEVEL changed; then the diagnostic would blame the wrong stage")
        problems += 1
    if far["gate_rejected"] <= normal["gate_rejected"]:
        print("  FAIL: a distant speaker did not make the gate reject more of the same speech, so"
              " the diagnostic would not point at the gate")
        problems += 1
    if far["accepted"] >= normal["accepted"]:
        print("  FAIL: turning the speaker down did not reduce what was accepted")
        problems += 1
    else:
        print(f"  ok: the gate refused more and accepted less"
              f" (rejected {normal['gate_rejected']} -> {far['gate_rejected']},"
              f" accepted {normal['accepted']} -> {far['accepted']})")

    # --------------------------------------------------- what the operator sees
    print("\nlog line, as it will appear in journalctl:")
    print(f"  {normal_lines[0]}")
    for field in ("room p20=", "p90=", "floor=", "thr=", "vad_speech=", "gate_rejected=",
                  "accepted=", "vad_speech_rms med="):
        if field not in normal_lines[0]:
            print(f"  FAIL: the logged line is missing {field}")
            problems += 1

    # ------------------------------------------------------ detector-side refusal
    # Room tone must not be blamed on the gate: the gate can only refuse what the detector offered.
    tone = array.array("h", [(i * 37 % 400) - 200 for i in range(rate * 2)]).tobytes()
    tone_lines, _ = run(tone)
    tone_totals = totals(tone_lines)
    print(f"\nquiet room tone: vad_speech={tone_totals['vad_speech']}"
          f"  gate_rejected={tone_totals['gate_rejected']}"
          f"  accepted={tone_totals['accepted']}")
    if tone_totals["gate_rejected"] > tone_totals["vad_speech"]:
        print("  FAIL: the gate refused more frames than the detector ever offered it")
        problems += 1

    print()
    if problems:
        print(f"=== FAILED: {problems} PROBLEM(S) ===")
        return 1
    print("=== ALL CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
