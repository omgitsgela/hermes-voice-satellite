#!/usr/bin/env python3
"""Microphone input-gain control checks (issue #6).

`AlsaMicGain` owns the INPUT level, the sibling of `volume_control` (which owns OUTPUT).
The property that matters: it shares the same honesty rule as volume - it never reports a
level it did not set and it never invents a control. On a card whose capture is an arecord
command line with no amixer input control, the correct behavior is to say so, not to pretend.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pi.mic_gain_control import (  # noqa: E402
    AlsaMicGain,
    MAX_PERCENT,
    VolumeGainError,
    parse_percent,
)

problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


class FakeAmixer:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        r = R()
        if "sget" in argv:
            r.stdout = f"Simple mixer control 'Mic',0\n  Capabilities: volume\n  Playback: " \
                       f"[{('100' if not self.fail else '0')}]%\n"
        if self.fail and "sset" in argv:
            r.returncode = 1
            r.stderr = "amixer: unable to write"
        return r


# ---------------------------------------------------------------- parse_percent

check("a normal percent is parsed", parse_percent("Capabilities: volume\n  [78%]") == 78)
check("a zero percent is parsed", parse_percent("[0%]") == 0)
check("a hundred percent is parsed", parse_percent("[100%]") == 100)
check("no bracketed percent yields None", parse_percent("Capabilities: volume") is None)
check("a 1000% bracket is rejected as unparseable", parse_percent("[1000%]") is None)
check("non-string input yields None", parse_percent(None) is None)

# ---------------------------------------------------------------- no control: honest, not a success

gain = AlsaMicGain(card="seeed2micvoicec", control="", runner=FakeAmixer())
check("an empty control means no control is present", gain.has_control() is False)
check("reading with no control yields None, not an error", gain.read() is None)
try:
    gain.set(50)
    raised = False
except VolumeGainError:
    raised = True
check("setting with no control raises rather than reporting success", raised)

# ---------------------------------------------------------------- present control: real set

fake = FakeAmixer()
gain = AlsaMicGain(card="seeed2micvoicec", control="Mic", runner=fake)
check("a named control is present", gain.has_control() is True)
applied = gain.set(60)
check("a set reports the level it told amixer", applied == 60, applied)
check("amixer was actually called with the level",
      any("sset" in c and "60%" in c for c in fake.calls), fake.calls)

# ---------------------------------------------------------------- clamping

fake = FakeAmixer()
gain = AlsaMicGain(card="seeed2micvoicec", control="Mic", runner=fake)
check("a value above 100 is clamped to 100", gain.set(140) == 100)
check("a value below 0 is clamped to 0", gain.set(-5) == 0)
check("MAX_PERCENT is 100", MAX_PERCENT == 100)

# ---------------------------------------------------------------- refusal is an error, not a success

fake = FakeAmixer(fail=True)
gain = AlsaMicGain(card="seeed2micvoicec", control="Mic", runner=fake)
try:
    gain.set(50)
    raised = False
except VolumeGainError:
    raised = True
check("an amixer refusal raises rather than reporting the level as applied", raised)

# ---------------------------------------------------------------- a bad type is refused

try:
    AlsaMicGain(card="c", control="Mic", runner=FakeAmixer()).set("forty")
    raised = False
except VolumeGainError:
    raised = True
check("a non-numeric gain is refused", raised)

print(f"mic gain checks: {checks - len(problems)}/{checks} passed")
for problem in problems:
    print(f"  FAIL: {problem}")
raise SystemExit(1 if problems else 0)
