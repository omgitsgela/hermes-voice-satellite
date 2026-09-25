#!/usr/bin/env python3
"""The virtual volume range (issue #10).

Angela's correction, 2026-09-25: the spoken scales must span the *usable* range, not
the raw one. On this hardware the measured floor is 50% of the ALSA range, so before
this change "set volume to 30%" was clamped to 50 and reported back as an error - half
the percentage scale was dead, and every value below it did the same thing.

Now both scales are proportions of the usable range:

    user  1% / level  1  -> raw floor (50 on this device)
    user 50%             -> raw 75   (halfway through what the hardware can do)
    user 100% / level 10 -> raw 100

The 1-10 scale is simply the same scale sampled at ten points, so the two agree by
construction rather than by two mappings that have to be kept in step.

Raw is what the ALSA control takes. User is what a person speaks and what the
configuration plane carries, so the API stays the same on hardware with a different
floor.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge.volume import (  # noqa: E402
    ALARM_MIN_PERCENT,
    FLOOR_PERCENT,
    MAX_PERCENT,
    USER_MIN,
    USER_MAX,
    clamp,
    parse_volume_command,
    raw_to_user_percent,
    resolve,
    ten_scale_to_percent,
    user_percent_to_raw,
)

problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


# ---------------------------------------------------------------- the user range

check("the user range starts at 1, so 0 is not a position on it", USER_MIN == 1, USER_MIN)
check("the user range ends at 100", USER_MAX == 100, USER_MAX)

# ---------------------------------------------------------------- endpoints

check("user 1% is the measured floor, not 1% of the raw range",
      user_percent_to_raw(USER_MIN) == FLOOR_PERCENT, user_percent_to_raw(USER_MIN))
check("user 100% is the top of the raw range",
      user_percent_to_raw(USER_MAX) == MAX_PERCENT, user_percent_to_raw(USER_MAX))

# ---------------------------------------------------------------- the midpoint, which is the point

mid = (FLOOR_PERCENT + MAX_PERCENT) // 2
check(f"user 50% is halfway through the USABLE range (raw {mid}), not raw 50",
      abs(user_percent_to_raw(50) - mid) <= 1,
      f"raw {user_percent_to_raw(50)} vs expected ~{mid}")

# ---------------------------------------------------------------- monotonic and bounded

check("the mapping is monotonic",
      all(user_percent_to_raw(n) <= user_percent_to_raw(n + 1) for n in range(USER_MIN, USER_MAX)))
check("no user value can produce a raw level below the floor",
      all(user_percent_to_raw(n) >= FLOOR_PERCENT for n in range(USER_MIN, USER_MAX + 1)))
check("no user value can exceed full volume",
      all(user_percent_to_raw(n) <= MAX_PERCENT for n in range(USER_MIN, USER_MAX + 1)))

# ---------------------------------------------------------------- the two scales agree

# The 1-10 scale must be the same scale, sampled ten times. If these ever diverge,
# "volume 5" and "volume 44%" would disagree, which is exactly the kind of thing a
# user notices and cannot explain.
check("level 1 and user 1% are the same level",
      ten_scale_to_percent(1) == user_percent_to_raw(1),
      (ten_scale_to_percent(1), user_percent_to_raw(1)))
check("level 10 and user 100% are the same level",
      ten_scale_to_percent(10) == user_percent_to_raw(100),
      (ten_scale_to_percent(10), user_percent_to_raw(100)))
for level in range(1, 11):
    implied_user = 1 + (level - 1) * (USER_MAX - USER_MIN) // 9
    check(f"level {level} equals user {implied_user}%",
          abs(ten_scale_to_percent(level) - user_percent_to_raw(implied_user)) <= 1,
          f"level {level} -> raw {ten_scale_to_percent(level)}, "
          f"user {implied_user}% -> raw {user_percent_to_raw(implied_user)}")

# ---------------------------------------------------------------- round tripping

for n in (1, 2, 25, 50, 75, 99, 100):
    raw = user_percent_to_raw(n)
    back = raw_to_user_percent(raw)
    check(f"user {n}% survives a round trip through raw", abs(back - n) <= 1, f"{n} -> {raw} -> {back}")

check("raw reading the floor reports user 1%",
      raw_to_user_percent(FLOOR_PERCENT) == USER_MIN, raw_to_user_percent(FLOOR_PERCENT))
check("raw reading full reports user 100%",
      raw_to_user_percent(MAX_PERCENT) == USER_MAX, raw_to_user_percent(MAX_PERCENT))

# ---------------------------------------------------------------- what the user hears

# "set volume to 30%" must now MEAN 30%, landing at raw 65 - not be clamped to the floor.
intent = parse_volume_command("set volume to 30%")
check("30% is a valid request now, not a clamp",
      intent is not None and intent.percent == 30 and intent.clamped is False,
      intent)
check("30% maps to raw 65 (30% of the usable range above the floor)",
      abs(user_percent_to_raw(intent.percent) - (FLOOR_PERCENT + 15)) <= 1,
      user_percent_to_raw(intent.percent))

# The floor itself is requestable and is NOT flagged as clamped.
intent = parse_volume_command("set volume to 1%")
check("1% is accepted as the quietest level, not reported as a clamp",
      intent is not None and intent.percent == 1 and intent.clamped is False, intent)
check("1% lands exactly on the floor",
      user_percent_to_raw(intent.percent) == FLOOR_PERCENT, user_percent_to_raw(intent.percent))

# Zero is still refused - that is the whole rule.
intent = parse_volume_command("set volume to 0")
check("0 is still refused", intent is not None and intent.clamped is True, intent)
check("0 is raised to the quietest level, which is user 1%",
      intent is not None and intent.percent == USER_MIN, intent)

intent = parse_volume_command("set volume to 0%")
check("0% is still refused", intent is not None and intent.percent == USER_MIN and intent.clamped,
      intent)

# Nothing is above the top.
intent = parse_volume_command("set volume to 150%")
check("a request above 100% is capped rather than rejected",
      intent is not None and intent.percent == USER_MAX, intent)

# ---------------------------------------------------------------- the spoken reply

_, say = resolve(parse_volume_command("set volume to 30%"), 50)
check("the reply speaks the user's own number", "30" in say, say)
_, say = resolve(parse_volume_command("set volume to 1%"), 50)
check("the reply at the quietest level does not claim a clamp", "silent" not in say.lower(), say)
_, say = resolve(parse_volume_command("set volume to 0"), 50)
check("the reply to 0 explains itself", "silent" in say.lower() or "zero" in say.lower(), say)

# ---------------------------------------------------------------- the guarantee still holds

requests = ["set volume to 0", "set volume to 0%", "set volume to 1%", "volume down",
            "volume down by 9", "quieter", "set volume to 2%"]
for text in requests:
    for current in (0, 1, 2, 50, 99, 100, -10, 1000):
        intent = parse_volume_command(text)
        if intent is None:
            continue
        level, _ = resolve(intent, current)
        check(f"{text!r} from {current} stays within the user range",
              USER_MIN <= level <= USER_MAX, f"got {level}")
        check(f"{text!r} from {current} cannot reach silence once mapped",
              user_percent_to_raw(level) >= FLOOR_PERCENT,
              f"user {level} -> raw {user_percent_to_raw(level)}")

# And clamp() on a raw value is still the last line of defence for anything that
# bypasses the user scale entirely.
check("raw clamp still refuses zero", clamp(0) == FLOOR_PERCENT, clamp(0))
check("raw clamp still refuses below the floor", clamp(FLOOR_PERCENT - 1) == FLOOR_PERCENT)

# ---------------------------------------------------------------- the alarm override

check("the alarm minimum is expressed on the same user scale",
      USER_MIN <= ALARM_MIN_PERCENT <= USER_MAX, ALARM_MIN_PERCENT)

print(f"virtual range checks: {checks - len(problems)}/{checks} passed")
for problem in problems:
    print(f"  FAIL: {problem}")
raise SystemExit(1 if problems else 0)
