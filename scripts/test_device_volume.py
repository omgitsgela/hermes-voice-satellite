#!/usr/bin/env python3
"""Device-side ALSA volume checks (issue #10).

These run offline against a fake `amixer`, because the point is the parsing, the
clamp and the refusal to reach silence, not the hardware. The two values that DO
need hardware (which control the speaker follows, and the audible floor) are
established by scripts/calibrate-volume.py on the device.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pi.volume_control import AlsaVolume, VolumeCommandError, parse_percent  # noqa: E402

problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


# Real `amixer -c seeed2micvoicec sget PCM` shape, including the two-channel layout.
AMIXER_PCM_90 = """Simple mixer control 'PCM',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 255
  Mono:
  Front Left: Playback 229 [90%] [-1.00dB] [on]
  Front Right: Playback 229 [90%] [-1.00dB] [on]
"""

AMIXER_MUTED = """Simple mixer control 'PCM',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 255
  Mono:
  Front Left: Playback 0 [0%] [-51.00dB] [off]
  Front Right: Playback 0 [0%] [-51.00dB] [off]
"""

AMIXER_NO_PERCENT = """Simple mixer control 'PCM',0
  Capabilities: pvolume pswitch
  Limits: Playback 0 - 255
"""


class FakeAmixer:
    """Records every invocation and replays canned output."""

    def __init__(self, outputs: dict[str, str] | None = None, fail: bool = False) -> None:
        self.outputs = outputs or {}
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        result = Result()
        if self.fail:
            result.returncode = 1
            result.stderr = "amixer: Unable to find simple control"
            return result
        for key, output in self.outputs.items():
            if key in " ".join(argv):
                result.stdout = output
                return result
        # Nothing canned matched: a set is silent on success, and a get returns nothing.
        result.returncode = 1 if "sget" in argv else 0
        return result


# ---------------------------------------------------------------- parsing

check("parses a percentage out of a normal amixer read",
      parse_percent(AMIXER_PCM_90) == 90, parse_percent(AMIXER_PCM_90))
check("parses a muted, zero-percent read as 0",
      parse_percent(AMIXER_MUTED) == 0, parse_percent(AMIXER_MUTED))
check("returns None when there is no percentage to read",
      parse_percent(AMIXER_NO_PERCENT) is None, parse_percent(AMIXER_NO_PERCENT))
check("returns None for empty output rather than guessing",
      parse_percent("") is None, parse_percent(""))

# ---------------------------------------------------------------- read

fake = FakeAmixer({"sget": AMIXER_PCM_90})
vol = AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake)
check("read returns the device's current level", vol.read() == 90, vol.read())
check("read asks for the configured card and control",
      any("seeed2micvoicec" in c and "PCM" in c and "sget" in c for c in fake.calls), fake.calls)

fake_muted = FakeAmixer({"sget": AMIXER_MUTED})
vol_muted = AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake_muted)
check("read reports a muted device as 0", vol_muted.read() == 0, vol_muted.read())

fake_bad = FakeAmixer(fail=True)
vol_bad = AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake_bad)
check("read reports None when the control does not exist", vol_bad.read() is None, vol_bad.read())

# ---------------------------------------------------------------- set, and the floor

fake_set = FakeAmixer()
vol_set = AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake_set)
applied = vol_set.set(60)
check("set applies the requested level", applied == 60, applied)
check("set passes the level to amixer as a percentage",
      any("sset" in c and any("60%" in part for part in c) for c in fake_set.calls), fake_set.calls)

fake_zero = FakeAmixer()
vol_zero = AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake_zero)
applied = vol_zero.set(0)
check("set(0) is raised to the floor and never reaches the device as zero",
      applied == 25, applied)
check("set(0) never sends 0% to amixer",
      not any("0%" in part for c in fake_zero.calls for part in c), fake_zero.calls)
check("set(0) does send the floor", 
      any("25%" in part for c in fake_zero.calls for part in c), fake_zero.calls)

applied = vol_zero.set(-50)
check("a negative request is raised to the floor too", applied == 25, applied)

fake_below = FakeAmixer()
vol_below = AlsaVolume(card="seeed2micvoicec", control="PCM", floor=30, runner=fake_below)
check("a level below the floor is raised to it", vol_below.set(5) == 30, vol_below.set(5))

fake_high = FakeAmixer()
vol_high = AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake_high)
check("a level above maximum is capped at 100", vol_high.set(500) == 100, vol_high.set(500))

# The property that matters: no input can produce a level below the floor.
fake_prop = FakeAmixer()
vol_prop = AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake_prop)
for requested in (0, 1, 2, -1, -999, 24):
    check(f"set({requested}) cannot produce silence", vol_prop.set(requested) >= 25,
          vol_prop.set(requested))

# ---------------------------------------------------------------- failure handling

fake_fail = FakeAmixer(fail=True)
vol_fail = AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake_fail)
try:
    vol_fail.set(60)
    check("a failed amixer set raises rather than reporting a false success", False, "no raise")
except VolumeCommandError as exc:
    check("a failed amixer set raises rather than reporting a false success", True)
    check("the error explains what failed", "amixer" in str(exc).lower() or "control" in str(exc).lower(),
          str(exc))

# ---------------------------------------------------------------- persistence

fake_store = FakeAmixer()
vol_store = AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake_store)
check("store reports success", vol_store.store() is True)
check("store runs alsactl store, so the level survives a reboot",
      any("alsactl" in c[0] and "store" in c for c in fake_store.calls), fake_store.calls)

fake_store_fail = FakeAmixer(fail=True)
vol_store_fail = AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake_store_fail)
check("a failed store reports False rather than raising", vol_store_fail.store() is False)

print(f"device volume checks: {checks - len(problems)}/{checks} passed")
for problem in problems:
    print(f"  FAIL: {problem}")
raise SystemExit(1 if problems else 0)
