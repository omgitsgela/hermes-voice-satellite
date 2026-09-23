#!/usr/bin/env python3
"""Run ON the satellite, as root: add the prompt-tone flags to its systemd unit.

    scp scripts/enable_chimes_on_satellite.py wyoming1:/tmp/
    ssh wyoming1 'sudo python3 /tmp/enable_chimes_on_satellite.py'

Why the satellite owns the tone rather than the bridge: the satellite plays --awake-wav
locally the instant its wake word fires (no network round trip), and it mutes the
microphone for the wav's duration PLUS --mic-seconds-to-mute-after-awake-wav
(satellite.py:656-662), so the tone is never captured into the utterance. A
bridge-sent chime could not do the mute, and would land in the recorded audio.

Idempotent. Backs the unit up before editing. Never restarts anything itself.
"""

import shutil
import sys
import time
from pathlib import Path

UNIT = Path("/etc/systemd/system/wyoming-satellite.service")
SOUNDS = ["/home/wyoming1/sounds/awake.wav", "/home/wyoming1/sounds/done.wav"]
FLAGS = (
    " --awake-wav /home/wyoming1/sounds/awake.wav"
    " --done-wav /home/wyoming1/sounds/done.wav"
    " --mic-seconds-to-mute-after-awake-wav 0.6"
)

missing = [p for p in SOUNDS if not Path(p).is_file()]
if missing:
    # Fail closed: a bad --awake-wav path can stop the satellite from starting at all.
    sys.exit(f"REFUSING TO PATCH: missing sound files: {missing}")

text = UNIT.read_text()

if "--awake-wav" in text:
    print("already patched; no change made")
else:
    backup = UNIT.with_name(f"{UNIT.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(UNIT, backup)
    print(f"backup written: {backup}")
    patched_lines = []
    found = False
    for line in text.splitlines(keepends=True):
        if line.startswith("ExecStart="):
            line = line.rstrip("\n") + FLAGS + "\n"
            found = True
        patched_lines.append(line)
    if not found:
        sys.exit("REFUSING TO PATCH: no ExecStart= line found")
    UNIT.write_text("".join(patched_lines))
    print("ExecStart updated")

print("\n--- ExecStart now ---")
for line in UNIT.read_text().splitlines():
    if line.startswith("ExecStart="):
        # one flag per line, much easier to read than the original single line
        for token in line[len("ExecStart="):].split(" --"):
            token = token.strip()
            if token:
                print("   --" + token)
        break
