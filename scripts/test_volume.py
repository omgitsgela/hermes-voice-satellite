#!/usr/bin/env python3
"""Volume control checks (issue #10).

The rule under test, which is the whole point: **no spoken command may silence the
device.** A voice device at zero is unreachable, because you cannot talk it back up
if you cannot hear it confirm anything.

Run with the bridge interpreter:

    ~/hermes-voice-bridge/.venv/bin/python scripts/test_volume.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge.volume import (  # noqa: E402
    ALARM_MIN_PERCENT,
    FLOOR_PERCENT,
    MAX_PERCENT,
    VolumeError,
    clamp,
    parse_volume_command,
    resolve,
    step,
    ten_scale_to_percent,
)

problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


# ---------------------------------------------------------------- the floor

check("the floor is above zero, so no level can mean silence", FLOOR_PERCENT > 0, FLOOR_PERCENT)
check("the floor is a whole percentage", isinstance(FLOOR_PERCENT, int), FLOOR_PERCENT)
check("the floor leaves a usable range above it", FLOOR_PERCENT < MAX_PERCENT, FLOOR_PERCENT)
check("max is 100 percent", MAX_PERCENT == 100, MAX_PERCENT)
check("the alarm override sits at or above the floor",
      ALARM_MIN_PERCENT >= FLOOR_PERCENT, (ALARM_MIN_PERCENT, FLOOR_PERCENT))
check("the alarm override is at or below full volume",
      ALARM_MIN_PERCENT <= MAX_PERCENT, ALARM_MIN_PERCENT)

# clamp is the single choke point; every path must go through it.
check("clamp refuses zero", clamp(0) == FLOOR_PERCENT, clamp(0))
check("clamp refuses negative levels", clamp(-40) == FLOOR_PERCENT, clamp(-40))
check("clamp refuses a level below the floor", clamp(FLOOR_PERCENT - 1) == FLOOR_PERCENT)
check("clamp passes a level above the floor through", clamp(FLOOR_PERCENT + 5) == FLOOR_PERCENT + 5)
check("clamp caps above maximum", clamp(999) == MAX_PERCENT, clamp(999))
check("clamp is idempotent", clamp(clamp(0)) == clamp(0), clamp(clamp(0)))

# ---------------------------------------------------------------- the 1-10 scale

# The scale must span the USABLE range: 1 is the floor, not 10 percent. A "1" that
# meant near-silence would defeat the purpose of the scale.
check("level 1 on the ten scale is the floor, not near-silence",
      ten_scale_to_percent(1) == FLOOR_PERCENT, ten_scale_to_percent(1))
check("level 10 on the ten scale is full volume",
      ten_scale_to_percent(10) == MAX_PERCENT, ten_scale_to_percent(10))
check("the ten scale is monotonic",
      all(ten_scale_to_percent(n) < ten_scale_to_percent(n + 1) for n in range(1, 10)))
check("the ten scale never dips below the floor",
      all(ten_scale_to_percent(n) >= FLOOR_PERCENT for n in range(1, 11)))
check("the ten scale never exceeds the maximum",
      all(ten_scale_to_percent(n) <= MAX_PERCENT for n in range(1, 11)))
for bad in (0, 11, -1):
    try:
        ten_scale_to_percent(bad)
        check(f"the ten scale rejects {bad}", False, "accepted")
    except VolumeError:
        check(f"the ten scale rejects {bad}", True)

# ---------------------------------------------------------------- stepping

check("stepping up from the floor moves up", step(FLOOR_PERCENT, +1) > FLOOR_PERCENT)
check("stepping down from the floor stays at the floor, never zero",
      step(FLOOR_PERCENT, -1) == FLOOR_PERCENT, step(FLOOR_PERCENT, -1))
check("stepping down from just above the floor lands on the floor, not below it",
      step(FLOOR_PERCENT + 1, -1) >= FLOOR_PERCENT, step(FLOOR_PERCENT + 1, -1))
check("stepping up from maximum stays at maximum", step(MAX_PERCENT, +1) == MAX_PERCENT)
check("stepping by two works", step(FLOOR_PERCENT, +2) > step(FLOOR_PERCENT, +1))
check("a large downward step still cannot reach silence", step(MAX_PERCENT, -999) == FLOOR_PERCENT)

# ---------------------------------------------------------------- parsing

absolute_cases = [
    ("set volume to 7", ten_scale_to_percent(7)),
    ("set the volume to 3", ten_scale_to_percent(3)),
    ("volume to 10", ten_scale_to_percent(10)),
    ("set volume to 40%", 40),
    ("set volume to 40 percent", 40),
    ("volume 100 percent", 100),
    ("set volume to 65", 65),          # a bare number above the ten scale reads as a percentage
]
for text, expected in absolute_cases:
    result = parse_volume_command(text)
    check(f"{text!r} parses as an absolute set",
          result is not None and result.kind == "set" and result.percent == expected,
          f"got {result!r}, expected percent {expected}")

for text in ("what's the volume", "whats the volume", "what is the volume", "volume status"):
    result = parse_volume_command(text)
    check(f"{text!r} is a query", result is not None and result.kind == "query", result)

for text, direction in (("volume up", +1), ("volume down", -1),
                        ("turn the volume up", +1), ("turn the volume down", -1),
                        ("louder", +1), ("quieter", -1)):
    result = parse_volume_command(text)
    check(f"{text!r} is a relative step",
          result is not None and result.kind == "step" and result.direction == direction, result)

result = parse_volume_command("volume up by two")
check("'by two' becomes a two-step move",
      result is not None and result.kind == "step" and result.steps == 2, result)
result = parse_volume_command("volume down by 3")
check("'by 3' becomes a three-step move",
      result is not None and result.kind == "step" and result.steps == 3, result)

# Not volume commands: must not be swallowed away from the model.
#
# "turn it up" is here on purpose. It probably does mean the device volume, but "it"
# could be music playing on the same device and the bridge cannot tell the difference.
# A mis-heard bridge-local command is worse than the assistant taking an extra second,
# so the ambiguous form deliberately falls through.
for text in ("turn on follow up mode", "what is the weather", "play some music",
             "set a timer for 5 minutes", "turn it up", "turn it down", "", "   "):
    check(f"{text!r} is not treated as a volume command",
          parse_volume_command(text) is None, parse_volume_command(text))

# ------------------------------------------------------- the zero command (#10's headline)

result = parse_volume_command("set volume to 0")
check("'set volume to 0' is recognised as a command, not ignored", result is not None, result)
check("'set volume to 0' is clamped to the floor, not refused into silence",
      result is not None and result.kind == "set" and result.percent == FLOOR_PERCENT, result)
check("'set volume to 0' is flagged as clamped so the reply can say so",
      result is not None and result.clamped is True, result)

result = parse_volume_command("set volume to 0%")
check("'set volume to 0%' is also clamped, not ignored",
      result is not None and result.percent == FLOOR_PERCENT and result.clamped, result)

below = FLOOR_PERCENT - 1
if below >= 1:
    result = parse_volume_command(f"set volume to {below}%")
    check("a percentage below the floor is clamped too",
          result is not None and result.percent == FLOOR_PERCENT and result.clamped, result)

# A request that is already valid must NOT be reported as clamped, or the spoken
# reply would wrongly claim it changed the number.
result = parse_volume_command("set volume to 60%")
check("a level above the floor is not flagged as clamped",
      result is not None and result.clamped is False, result)

# ---------------------------------------------------------------- resolve + speak

# resolve() turns an intent into (new_percent, spoken_line) given the current level.

level, speech = resolve(parse_volume_command("set volume to 7"), MAX_PERCENT)
check("an absolute set lands on the requested level",
      level == ten_scale_to_percent(7), level)
check("the reply speaks the level in the user's own terms, as a ten-scale value",
      "7" in speech, speech)

level, speech = resolve(parse_volume_command("set volume to 40%"), MAX_PERCENT)
check("an absolute percentage lands on that percentage", level == 40, level)
check("a percentage reply mentions the percentage", "40" in speech, speech)

level, speech = resolve(parse_volume_command("volume up"), 50)
check("a relative step moves up from the current level", level > 50, level)
check("a step reply states the resulting level",
      str(level) in speech or "out of" in speech, speech)

level, speech = resolve(parse_volume_command("volume down"), FLOOR_PERCENT)
check("a downward step at the floor keeps the floor and does not go silent",
      level == FLOOR_PERCENT, level)
check("the reply says it is already at the quietest level",
      "quiet" in speech.lower() or "lowest" in speech.lower(), speech)

# The clamp must be spoken, not silent: the user asked for 0 and must learn why not.
level, speech = resolve(parse_volume_command("set volume to 0"), 60)
check("clamping to the floor still reports the resulting level", level == FLOOR_PERCENT, level)
check("the clamped reply explains that zero is not allowed",
      "silent" in speech.lower() or "zero" in speech.lower(), speech)

level, speech = resolve(parse_volume_command("what's the volume"), 42)
check("a query changes nothing", level == 42, level)
check("a query reply states the current level", "42" in speech, speech)

# Every reply is one short line, because it is spoken.
for text, current in (("set volume to 7", 50), ("set volume to 0", 50), ("what's the volume", 30),
                      ("volume up", FLOOR_PERCENT), ("volume down", FLOOR_PERCENT)):
    _, speech = resolve(parse_volume_command(text), current)
    check(f"the reply to {text!r} is one short line",
          "\n" not in speech and len(speech) <= 60, repr(speech))

# ---------------------------------------------------------------- no path to silence

# The property that matters most: whatever the current level and whatever the request,
# resolve() can never return a level that is zero or below the floor.
requests = ["set volume to 0", "set volume to 0%", "volume down", "volume down by 9",
            "quieter", "volume down by two", "set volume to 1%"]
for text in requests:
    for current in (0, 1, FLOOR_PERCENT, FLOOR_PERCENT + 1, 50, MAX_PERCENT, 1000, -10):
        intent = parse_volume_command(text)
        if intent is None:
            continue
        result_level, _ = resolve(intent, current)
        check(f"{text!r} from current={current} cannot reach silence",
              result_level >= FLOOR_PERCENT, f"got {result_level}")

# The alarm override is upward-only: it raises a too-quiet level and never lowers a loud one.
from bridge.volume import alarm_percent  # noqa: E402

check("an alarm raises a level that is too quiet to hear",
      alarm_percent(FLOOR_PERCENT) == max(ALARM_MIN_PERCENT, FLOOR_PERCENT),
      alarm_percent(FLOOR_PERCENT))
check("an alarm never lowers a level the user set louder",
      alarm_percent(MAX_PERCENT) == MAX_PERCENT, alarm_percent(MAX_PERCENT))
check("an alarm at the user's normal level is left alone",
      alarm_percent(90) == 90, alarm_percent(90))
check("an alarm can never be silent",
      alarm_percent(0) >= max(ALARM_MIN_PERCENT, FLOOR_PERCENT), alarm_percent(0))

print(f"volume checks: {checks - len(problems)}/{checks} passed")
for problem in problems:
    print(f"  FAIL: {problem}")
raise SystemExit(1 if problems else 0)
