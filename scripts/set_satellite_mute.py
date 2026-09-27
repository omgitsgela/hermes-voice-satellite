#!/usr/bin/env python3
"""Run ON the satellite, as root: set --mic-seconds-to-mute-after-awake-wav.

The satellite mutes the microphone for (awake wav duration + THIS value) after the wake
tone.

* Too large, and it eats the onset of the user's speech before it is ever recorded.
  Observed 2026-09-22: a 0.6 s margin (+ a 308 ms tone = 908 ms total) swallowed the front
  of the first word. The tell was a recording that opens at full level with no
  lead-in (first 20 ms peak 9416 against a 28982 overall peak).
* Too small, and the tone's acoustic tail leaks into the recording.

The chosen awake tone is 308 ms with an exponential decay, so 0.1 s covers the acoustic
tail without clipping speech: about 408 ms total.

    scp scripts/set_satellite_mute.py <satellite>:/tmp/
    ssh <satellite> 'sudo python3 /tmp/set_satellite_mute.py 0.1'
    ssh <satellite> 'sudo systemctl restart wyoming-satellite.service'

The value lives in the unit, so unlike the tone WAVs (re-read per detection) this needs a
service restart to take effect.
"""

import re
import sys
from pathlib import Path

UNIT = Path("/etc/systemd/system/wyoming-satellite.service")
FLAG = "--mic-seconds-to-mute-after-awake-wav"

if len(sys.argv) != 2:
    sys.exit(f"usage: {sys.argv[0]} <seconds>")
try:
    value = float(sys.argv[1])
except ValueError:
    sys.exit(f"not a number: {sys.argv[1]!r}")
if not 0 <= value <= 5:
    sys.exit("refusing: value must be between 0 and 5 seconds")

text = UNIT.read_text()
if FLAG not in text:
    sys.exit(f"REFUSING: {FLAG} is not in the unit; run enable_chimes_on_satellite.py first")

updated = re.sub(rf"{FLAG}\s+\S+", f"{FLAG} {value}", text)
if updated == text:
    print(f"already set to {value}")
else:
    UNIT.write_text(updated)
    print(f"set {FLAG} {value}")

print("\n--- relevant flags now ---")
for line in UNIT.read_text().splitlines():
    if line.startswith("ExecStart="):
        for token in line[len("ExecStart="):].split(" --"):
            token = token.strip()
            if "mute" in token or "wav" in token:
                print("   --" + token)
        break
