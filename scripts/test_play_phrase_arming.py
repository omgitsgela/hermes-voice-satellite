#!/usr/bin/env python3
"""play_phrase.py must arm the satellite before it plays (issue #37).

The script is the one people reach for to prove the speaker path works. Without RunSatellite first,
a satellite that is still streaming can ignore the first message it receives, so the script could
report failure on a system that is fine and send someone hunting a hardware fault.

This is a source-order check, deliberately: the script is a top-level program rather than an
importable module, so the behavioural test is running it against a real device. What this catches is
the regression that matters, someone deleting the arming line or moving it after the playback.

    .venv/bin/python scripts/test_play_phrase_arming.py
"""
from __future__ import annotations

import pathlib
import sys

SCRIPT = pathlib.Path(__file__).resolve().parent / "play_phrase.py"
source = SCRIPT.read_text()

checks = 0
failures = []


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(label + ": got " + repr(got) + ", wanted " + repr(want))


check("the script arms the satellite", "await sat.run()" in source, True)
check("it arms exactly once", source.count("await sat.run()"), 1)
check("and it still plays the audio", "sat.play_wav(" in source, True)

if "await sat.run()" in source and "sat.play_wav(" in source:
    check("arming comes before playback",
          source.index("await sat.run()") < source.index("sat.play_wav("), True)

# No check for a second RunSatellite beyond the call-site count above: mentioning the message name
# in a comment is not a second send, and counting text rather than calls gets that wrong.

print("play_phrase arming checks: " + str(checks - len(failures)) + "/" + str(checks) + " passed")
for f in failures:
    print("  FAIL: " + f)
raise SystemExit(1 if failures else 0)
