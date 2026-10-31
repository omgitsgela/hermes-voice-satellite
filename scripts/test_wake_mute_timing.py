#!/usr/bin/env python3
"""The deaf window in front of a wake must not swallow the first word.

Two clocks run when the wake word fires, and both are time when the person's voice is not being
recorded at all:

* the DEVICE mutes its microphone for the awake tone's duration plus `VS_MIC_MUTE_AFTER_AWAKE`
  (`satellite.py::_play_wav`), so the tone and its acoustic tail are never captured as speech;
* the BRIDGE ignores the opening `WAKE_QUIET_SECONDS` of the activation and drops whatever is
  already queued, its own share of the same problem (issue #47).

The tradeoff was measured on 2026-09-22 from the first samples of real captures:

    | capture                  | first 4 ms peaks       | first loud sample | verdict        |
    | before tones (control)   | 210, 186, 188, 118     | 0.066 s           | natural onset  |
    | mute 908 ms              | 6886, 7620, 8777, 8009 | 0.000 s           | opens mid-word |
    | mute 408 ms (0.1 margin) | 92, 142, 140, 172      | 0.537 s           | natural onset  |

A recording that opens at full level with no lead-in is the signature of a clipped onset. The tuned
value was 408 ms total; 908 ms ate the front of the first word.

It regressed on 2026-09-29. The margin had been raised to 0.4 s (708 ms total) during the issue #47
chime work, so "New conversation" was transcribed as "conversation": the first word fell inside the
window, the phrase matcher needs the whole phrase, and the request was answered by the model
instead of rotating the session. The tell was the capture length for identical words collapsing
from 1.0-2.1 s (2026-09-22, working) to 0.4-0.7 s.

Two invariants, one per clock:

1. the total window stays at or under the measured-good bound, so the onset is never clipped;
2. the Gateway's ignore window is no longer than the device's mute, because a drain that outlasts
   the mute discards audio the device is already sending (that is how the first word vanished).

    .venv/bin/python scripts/test_wake_mute_timing.py
"""
from __future__ import annotations

import re
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.config import WAKE_QUIET_SECONDS  # noqa: E402

# Measured-good total from 2026-09-22, with a little room for a deliberate retune. 408 ms gave a
# natural onset; 708 ms clipped it on 2026-09-29. Nothing may push the window back over this.
MAX_TOTAL_DEAF_SECONDS = 0.45

checks = 0
failures = []


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def tone_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def main() -> int:
    unit = (ROOT / "pi" / "systemd" / "wyoming-satellite.service").read_text()
    env = (ROOT / "pi" / "systemd" / "satellite.env").read_text()

    # The margin has to be the value the unit actually passes, or editing it changes nothing.
    match = re.search(r"^VS_MIC_MUTE_AFTER_AWAKE=(\S+)$", env, re.MULTILINE)
    check("satellite.env carries the margin", match is not None, True)
    if match is None:
        return report()
    margin = float(match.group(1))
    check("  it is a sane margin", 0.0 <= margin <= 1.0, True)
    check("  and the unit passes it through",
          '--mic-seconds-to-mute-after-awake-wav "$VS_MIC_MUTE_AFTER_AWAKE"' in unit, True)

    # The tone we measure must be the tone the satellite plays.
    awake = re.search(r'--awake-wav\s+"?([^"\s]+)"?', unit)
    check("the unit names an awake tone", awake is not None, True)
    if awake is not None:
        check("  and it is the tone this test measures",
              Path(awake.group(1)).name, "awake.wav")

    tone = tone_seconds(ROOT / "sounds" / "awake.wav")
    total = tone + margin

    # Invariant 1: the onset must survive the device's own mute.
    check(f"total device deaf window ({total:.3f}s) is within the measured-good bound",
          total <= MAX_TOTAL_DEAF_SECONDS, True)

    # Invariant 2: the Gateway must not listen less than the device is willing to talk. A drain that
    # runs past the mute throws away live audio, which is exactly how the first word disappeared.
    check(f"the Gateway's ignore window ({WAKE_QUIET_SECONDS:.3f}s) does not outlast the mute",
          WAKE_QUIET_SECONDS <= total, True)
    check("  and the ignore window still exists", WAKE_QUIET_SECONDS > 0.0, True)

    return report()


def report() -> int:
    print(f"wake mute timing checks: {checks - len(failures)}/{checks} passed")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


raise SystemExit(main())
