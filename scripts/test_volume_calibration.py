#!/usr/bin/env python3
"""Calibration round-trip checks (issue #10).

The calibration script runs on the Pi and is deliberately standalone; the two readers
live in different packages on different machines. Nothing but the JSON contract holds
them together, so this test pins that contract:

  writer (scripts/calibrate-volume.py)  ->  readers (bridge/volume.py, pi/volume_control.py)

A drift here would mean a measured floor silently ignored, which reads as "the volume
feature does not work" rather than "the file format changed".
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


# ---------------------------------------------------------- the script is standalone

script = ROOT / "scripts" / "calibrate-volume.py"
check("the calibration script exists", script.is_file(), script)

source = script.read_text()
for forbidden in ("from bridge", "from pi", "import bridge", "import pi"):
    check(f"the script does not import the repo ({forbidden!r}), so it can be copied alone",
          forbidden not in source, forbidden)

# It must at least parse and print help without the repo importable, which is the state
# it will be in on the device.
result = subprocess.run(
    [sys.executable, str(script), "--help"],
    capture_output=True, text=True, timeout=30,
    cwd=tempfile.gettempdir(), env={**os.environ, "PYTHONPATH": ""},
)
check("--help runs with no repo on the path", result.returncode == 0, result.stderr[-300:])
check("--help documents the phases",
      all(flag in result.stdout for flag in ("--enumerate", "--probe", "--floor", "--show")),
      result.stdout[:200])

# ---------------------------------------------------------- writer -> readers

spec = importlib.util.spec_from_file_location("calibrate_volume", script)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory() as tmp:
    target = pathlib.Path(tmp) / "volume.json"
    module.CALIBRATION_FILE = target
    module.STATE_DIR = pathlib.Path(tmp)
    module.write_calibration(31, "HP DAC", "seeed2micvoicec", alarm_min=65)

    check("the calibration file was written", target.is_file(), target)
    check("the calibration file is private (0600)",
          os.stat(target).st_mode & 0o777 == 0o600, oct(os.stat(target).st_mode & 0o777))

    written = json.loads(target.read_text())
    check("the written floor is present", written.get("floor_percent") == 31, written)
    check("the written control is present", written.get("control") == "HP DAC", written)
    check("the written card is present", written.get("card") == "seeed2micvoicec", written)
    check("the written alarm minimum is present", written.get("alarm_min_percent") == 65, written)

    # ---- reader 1: the device side
    from pi.volume_control import load_calibration

    floor, card, control = load_calibration(target)
    check("the device reader picks up the measured floor", floor == 31, floor)
    check("the device reader picks up the measured control", control == "HP DAC", control)
    check("the device reader picks up the card", card == "seeed2micvoicec", card)

    # ---- reader 2: the bridge side
    import bridge.volume as bridge_volume

    original = bridge_volume.CALIBRATION_FILE
    bridge_volume.CALIBRATION_FILE = target
    try:
        floor, alarm, card2, control2 = bridge_volume._load_calibration()
        check("the bridge reader picks up the measured floor", floor == 31, floor)
        check("the bridge reader picks up the alarm minimum", alarm == 65, alarm)
        check("the bridge reader picks up the control", control2 == "HP DAC", control2)
        check("the bridge reader picks up the card", card2 == "seeed2micvoicec", card2)

        # ---- and a measurement actually changes the clamping behaviour
        #
        # The calibrated values are module constants on purpose: clamp() is on the hot
        # path for every announcement and timer and must not read the filesystem. So a
        # calibration change takes effect through an explicit reload, and this pins that
        # the reload is what makes it real. Without this check, a measured floor could sit
        # on disk being ignored while every test still passed.
        applied_after_reload = bridge_volume.reload_calibration()
        check("reload adopts the measured floor", applied_after_reload[0] == 31, applied_after_reload)
        check("clamp now refuses anything below the measured floor",
              bridge_volume.clamp(0) == 31, bridge_volume.clamp(0))
        check("clamp still permits a level above the measured floor",
              bridge_volume.clamp(80) == 80, bridge_volume.clamp(80))
    finally:
        bridge_volume.CALIBRATION_FILE = original
        bridge_volume.reload_calibration()

    # After restoring, the module must be back to whatever the real machine has.
    check("restoring the calibration path restores the values",
          bridge_volume.FLOOR_PERCENT == bridge_volume._load_calibration()[0],
          bridge_volume.FLOOR_PERCENT)

# ---------------------------------------------------------- bad input falls back safely

with tempfile.TemporaryDirectory() as tmp:
    bad = pathlib.Path(tmp) / "volume.json"
    from pi.volume_control import DEFAULT_FLOOR_PERCENT, load_calibration

    # Absent
    floor, _card, _control = load_calibration(bad)
    check("a missing calibration falls back to the placeholder floor",
          floor == DEFAULT_FLOOR_PERCENT, floor)

    # Not JSON
    bad.write_text("{ this is not json")
    floor, _card, _control = load_calibration(bad)
    check("a malformed calibration falls back rather than raising",
          floor == DEFAULT_FLOOR_PERCENT, floor)

    # Out of range: a floor of 0 would be silence, which is the failure being prevented.
    bad.write_text(json.dumps({"floor_percent": 0, "control": "PCM", "card": "c"}))
    floor, _card, _control = load_calibration(bad)
    check("a calibrated floor of 0 is rejected in favour of the placeholder",
          floor == DEFAULT_FLOOR_PERCENT, floor)

    # A negative floor is equally unacceptable.
    bad.write_text(json.dumps({"floor_percent": -5}))
    floor, _card, _control = load_calibration(bad)
    check("a negative calibrated floor is rejected", floor == DEFAULT_FLOOR_PERCENT, floor)

    # 100 would leave no range above it.
    bad.write_text(json.dumps({"floor_percent": 100}))
    floor, _card, _control = load_calibration(bad)
    check("a calibrated floor of 100 is rejected", floor == DEFAULT_FLOOR_PERCENT, floor)

    # A valid one is still honoured after all that.
    bad.write_text(json.dumps({"floor_percent": 12, "control": "Line", "card": "c"}))
    floor, card, control = load_calibration(bad)
    check("a later valid calibration is honoured", (floor, card, control) == (12, "c", "Line"),
          (floor, card, control))

print(f"calibration checks: {checks - len(problems)}/{checks} passed")
for problem in problems:
    print(f"  FAIL: {problem}")
raise SystemExit(1 if problems else 0)
