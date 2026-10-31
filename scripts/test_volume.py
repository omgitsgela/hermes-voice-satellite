#!/usr/bin/env python3
"""Volume parsing, the raw clamp, and the alarm override (issue #10).

Companion to `test_volume_virtual_range.py`, which owns the user/raw mapping and the
"both scales agree" property. This file covers what that one does not:

  * which utterances are volume commands at all, and which must fall through to the model
  * the raw `clamp()`, the last line of defence for anything bypassing the user scale
  * the 1-10 scale's raw values and its rejection of nonsense
  * the alarm override's upward-only behaviour
  * that every reply is one short spoken line

Run with the Gateway interpreter:

    ~/hermes-voice-satellite/.venv/bin/python scripts/test_volume.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gateway.volume import (  # noqa: E402
    ALARM_MIN_PERCENT,
    FLOOR_PERCENT,
    MAX_PERCENT,
    USER_MAX,
    USER_MIN,
    VolumeError,
    alarm_percent,
    clamp,
    parse_volume_command,
    raw_to_user_percent,
    resolve,
    ten_scale_to_percent,
    user_for_ten_level,
    user_percent_to_raw,
)

problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


# ---------------------------------------------------------------- raw sanity

check("the raw floor is above zero", FLOOR_PERCENT > 0, FLOOR_PERCENT)
check("the raw floor leaves usable room above it", FLOOR_PERCENT < MAX_PERCENT, FLOOR_PERCENT)
check("full volume is 100", MAX_PERCENT == 100, MAX_PERCENT)


# ---------------------------------------------------------------- the raw clamp

# The user scale maps USER_MIN..USER_MAX onto floor..100, so nothing a person says can
# produce a raw value below the floor. This clamp is for everything that does NOT go
# through the user scale: a direct configuration-plane write, a future caller, a bug.
check("clamp refuses zero", clamp(0) == FLOOR_PERCENT, clamp(0))
check("clamp refuses a negative", clamp(-40) == FLOOR_PERCENT, clamp(-40))
check("clamp refuses just below the floor", clamp(FLOOR_PERCENT - 1) == FLOOR_PERCENT)
check("clamp passes a level above the floor", clamp(FLOOR_PERCENT + 5) == FLOOR_PERCENT + 5)
check("clamp caps above maximum", clamp(999) == MAX_PERCENT, clamp(999))
check("clamp is idempotent on a refusal", clamp(clamp(0)) == clamp(0))
check("clamp refuses a non-number rather than coercing it",
      clamp("loud") == FLOOR_PERCENT and clamp(None) == FLOOR_PERCENT,
      (clamp("loud"), clamp(None)))
check("clamp refuses a boolean rather than treating True as 1",
      clamp(True) == FLOOR_PERCENT, clamp(True))


# ---------------------------------------------------------------- the 1-10 scale, in raw

check("level 1 is the raw floor", ten_scale_to_percent(1) == FLOOR_PERCENT,
      ten_scale_to_percent(1))
check("level 10 is full raw volume", ten_scale_to_percent(10) == MAX_PERCENT,
      ten_scale_to_percent(10))
check("the scale is strictly increasing",
      all(ten_scale_to_percent(n) < ten_scale_to_percent(n + 1) for n in range(1, 10)),
      [ten_scale_to_percent(n) for n in range(1, 11)])
check("no level maps below the raw floor",
      all(ten_scale_to_percent(n) >= FLOOR_PERCENT for n in range(1, 11)))
check("no level maps above raw maximum",
      all(ten_scale_to_percent(n) <= MAX_PERCENT for n in range(1, 11)))

for bad in (0, 11, -1, 1.5, True, "5", None):
    try:
        ten_scale_to_percent(bad)
        check(f"the ten scale rejects {bad!r}", False, "accepted")
    except VolumeError:
        check(f"the ten scale rejects {bad!r}", True)


# ---------------------------------------------------------------- parsed forms

# Absolute, ten-scale. The intent carries the USER level: level 7 is user 67, which maps
# to raw 83 on this device. The two are different numbers and mixing them up is the bug
# this whole file exists to catch.
for text, user_level in (("set volume to 7", user_for_ten_level(7)),
                         ("set the volume to 3", user_for_ten_level(3)),
                         ("volume to 10", USER_MAX)):
    intent = parse_volume_command(text)
    check(f"{text!r} parses as an absolute ten-scale set at user {user_level}",
          intent is not None and intent.kind == "set" and intent.percent == user_level,
          intent)

# Absolute, percentage: a position on the user range, taken as asked.
for text, user_level in (("set volume to 40%", 40), ("set volume to 40 percent", 40),
                         ("volume 100 percent", 100), ("set volume to 65", 65),
                         ("set volume to 30%", 30)):
    intent = parse_volume_command(text)
    check(f"{text!r} parses as user {user_level}%",
          intent is not None and intent.kind == "set" and intent.percent == user_level,
          intent)

# Relative
for text, direction in (("volume up", +1), ("volume down", -1),
                        ("turn the volume up", +1), ("turn the volume down", -1),
                        ("louder", +1), ("quieter", -1)):
    intent = parse_volume_command(text)
    check(f"{text!r} is a relative step",
          intent is not None and intent.kind == "step" and intent.direction == direction,
          intent)

# Queries
for text in ("what's the volume", "whats the volume", "what is the volume", "volume status"):
    intent = parse_volume_command(text)
    check(f"{text!r} is a query", intent is not None and intent.kind == "query", intent)

# Step counts
intent = parse_volume_command("volume up by two")
check("'by two' becomes two steps",
      intent is not None and intent.kind == "step" and intent.steps == 2, intent)
intent = parse_volume_command("volume down by 3")
check("'by 3' becomes three steps",
      intent is not None and intent.kind == "step" and intent.steps == 3, intent)


# ---------------------------------------------------------------- what is NOT a volume command

# Conservative on purpose: a bridge-local command that changes the room on a mis-heard
# phrase is worse than the assistant taking an extra second. "turn it up" is here because
# "it" could be music playing on the same device and the Gateway cannot tell.
for text in ("turn on follow up mode", "what is the weather", "play some music",
             "set a timer for 5 minutes", "turn it up", "turn it down", "", "   ",
             "stop", "what time is it", "the volume of the box", "spin up a server"):
    check(f"{text!r} is not treated as a volume command",
          parse_volume_command(text) is None, parse_volume_command(text))

check("a non-string is not a volume command", parse_volume_command(None) is None)


# ---------------------------------------------------------------- zero, refused

intent = parse_volume_command("set volume to 0")
check("'set volume to 0' is recognised, not ignored", intent is not None, intent)
check("'set volume to 0' lands on the quietest USER level",
      intent is not None and intent.percent == USER_MIN, intent)
check("'set volume to 0' is flagged so the reply can explain",
      intent is not None and intent.clamped is True, intent)

intent = parse_volume_command("set volume to 0%")
check("'set volume to 0%' is refused the same way",
      intent is not None and intent.percent == USER_MIN and intent.clamped, intent)

# The quietest level itself is a normal request, NOT a clamp: it is a position on the
# scale. Getting this wrong would make the reply claim it changed a number it did not.
intent = parse_volume_command("set volume to 1%")
check("1% is an ordinary request, not a clamp",
      intent is not None and intent.percent == USER_MIN and intent.clamped is False, intent)

# Above the top is a silent cap: asking for more than full plainly means "full".
intent = parse_volume_command("set volume to 150%")
check("above 100% is capped, not refused",
      intent is not None and intent.percent == USER_MAX, intent)


# ---------------------------------------------------------------- stepping behaves

check("stepping up from the quietest moves up",
      user_for_ten_level(2) > USER_MIN, user_for_ten_level(2))
check("stepping down from the quietest stays at the quietest, never past it",
      resolve(parse_volume_command("volume down"), USER_MIN)[0] == USER_MIN)
check("stepping up from full stays at full",
      resolve(parse_volume_command("volume up"), USER_MAX)[0] == USER_MAX)


# ---------------------------------------------------------------- the alarm override

check("an alarm minimum sits inside the user range",
      USER_MIN <= ALARM_MIN_PERCENT <= USER_MAX, ALARM_MIN_PERCENT)
check("an alarm raises a level too quiet to hear",
      alarm_percent(USER_MIN) == max(ALARM_MIN_PERCENT, USER_MIN), alarm_percent(USER_MIN))
check("an alarm never lowers a level the user set louder",
      alarm_percent(USER_MAX) == USER_MAX, alarm_percent(USER_MAX))
check("an alarm leaves a loud room alone",
      alarm_percent(USER_MAX - 1) == USER_MAX - 1, alarm_percent(USER_MAX - 1))
check("an alarm cannot be silent",
      alarm_percent(0) >= ALARM_MIN_PERCENT, alarm_percent(0))
check("an alarm maps to more than the raw floor, so it is actually louder",
      user_percent_to_raw(alarm_percent(USER_MIN)) > FLOOR_PERCENT,
      user_percent_to_raw(alarm_percent(USER_MIN)))


# ---------------------------------------------------------------- reply shape

# Every reply is spoken aloud, so it is one short line. It reports the resulting level in
# the terms the person used: a ten-scale request is answered on the ten scale, a
# percentage in percent. So this checks the SPOKEN form, not the internal user number -
# asserting the reply contains the user level would wrongly demand "Volume 67 out of 10".
spoken_cases = [
    ("set volume to 7", 50, "7"),          # ten-scale request, ten-scale answer
    ("set volume to 30%", 50, "30"),       # percentage request, percentage answer
    ("what's the volume", 30, "30"),       # the query reports the current percentage
    ("set volume to 1%", 50, "1"),
]
for text, current, expect in spoken_cases:
    level, say = resolve(parse_volume_command(text), current)
    check(f"the reply to {text!r} is one short line",
          "\n" not in say and 0 < len(say) <= 60, repr(say))
    check(f"the reply to {text!r} speaks the resulting level as {expect!r}",
          expect in say, f"level {level} (user), said {say!r}")

# The two cases with nothing to report say exactly that, and name no number at all.
check("a downward step already at the bottom says so",
      "quietest" in resolve(parse_volume_command("volume down"), USER_MIN)[1].lower(),
      resolve(parse_volume_command("volume down"), USER_MIN)[1])
check("an upward step already at the top says so",
      "full" in resolve(parse_volume_command("volume up"), USER_MAX)[1].lower(),
      resolve(parse_volume_command("volume up"), USER_MAX)[1])

# Zero is refused with an explanation, never silently obeyed.
_, say = resolve(parse_volume_command("set volume to 0"), 50)
check("the reply to zero explains why it was not obeyed",
      "silent" in say.lower() or "zero" in say.lower(), repr(say))
check("the reply to zero is still one short line",
      "\n" not in say and len(say) <= 60, repr(say))

print(f"volume checks: {checks - len(problems)}/{checks} passed")
for problem in problems:
    print(f"  FAIL: {problem}")
raise SystemExit(1 if problems else 0)
