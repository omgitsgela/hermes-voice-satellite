#!/usr/bin/env python3
"""Offline tests for scripts/device-check.py (issue #34).

The checker runs on a device, but its classification logic is what it gets judged on, so it is
tested here with synthetic ALSA dumps and a stubbed command runner. Run with the Gateway venv:

    .venv/bin/python scripts/test_device_check.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("device_check", HERE / "device-check.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

checks = 0
failures = []


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


# 1. parsing the hardware ranges out of a dump
hw = mod.parse_hw_params("RATE: [16000 48000]\nCHANNELS: [2 2]\nFORMAT: S16_LE")
check("stereo-only rates", hw["rates"], (16000, 48000))
check("stereo-only channels", hw["channels"], (2, 2))

hw = mod.parse_hw_params("ACCESS: MMAP_INTERLEAVED\nRATE: [8000 48000]\nCHANNELS: [1 2]\n")
check("mono+stereo channels", hw["channels"], (1, 2))
check("wide rates", hw["rates"], (8000, 48000))

hw = mod.parse_hw_params("RATE: [44100 48000]")
lo, hi = hw["rates"]
check("16k not inside the advertised range", lo <= 16000 <= hi, False)
check("range still parsed", (lo, hi), (44100, 48000))
check("missing channels stays None", hw["channels"], None)

# 2. classification against a stubbed runner: the case in the issue, a stereo-only card
DUMP = "RATE: [16000 48000]\nCHANNELS: [2 2]\nFORMAT: S16_LE"
mod.run = lambda cmd, timeout=10: DUMP if "--dump-hw-params" in cmd else ""
caps = mod.card_caps("fakecard")
check("stereo-only: mono unavailable", caps["capture_mono"], False)
check("stereo-only: stereo available", caps["capture_stereo"], True)

# ... and the same card must not be counted as usable for a mono stack without a channel choice
# (the old code returned True here, which is the misclassification in #34)
check("stereo-only: 16k mono not claimed", caps["capture_16k_mono"], False)

# 3. a mono card still passes
DUMP2 = "RATE: [8000 48000]\nCHANNELS: [1 2]"
mod.run = lambda cmd, timeout=10: DUMP2 if "--dump-hw-params" in cmd else ""
caps = mod.card_caps("fakecard")
check("mono card: 16k mono available", caps["capture_16k_mono"], True)
check("mono card: mono reported", caps["capture_mono"], True)

# 4. a card that cannot do 16 kHz is reported as such
mod.run = lambda cmd, timeout=10: ("RATE: [44100 48000]\nCHANNELS: [1 2]" if "--dump-hw-params" in cmd else "")
check("no 16k: not available", mod.card_caps("fakecard")["capture_16k_mono"], False)

# 5. busy is still not a failure
mod.run = lambda cmd, timeout=10: "arecord: main:1234: audio open error: Device or resource busy"
caps = mod.card_caps("fakecard")
check("busy is named", caps["busy"], True)
check("busy is not measured", caps["measured_16k_mono"], None)

print(f"device-check checks: {checks - len(failures)}/{checks} passed")
for f in failures:
    print("  FAIL:", f)
sys.exit(1 if failures else 0)
