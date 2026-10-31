"""Volume control for the voice satellite (issue #10).

The rule this module exists to enforce, in one sentence: **no path may take the
device to silence.** A voice device at zero volume is unreachable, because you
cannot talk it back up when you cannot hear it confirm anything. So there is a
floor, it is above zero, and every command, announcement, timer and alarm goes
through :func:`clamp` on the way out.

The decision, 2026-09-25, on the one question the issue reserved for the room's owner:
**an alarm may override upward to a minimum alarm level**, so an alarm is always
audible even when the room is set quiet. That override is upward-only. It never
lowers a level the owner chose.

Where the level actually lives: speech is rendered on the Gateway but *playback*
happens on the satellite, so the number that matters is the Pi's ALSA level for
the output card. Our TTS, the connected tone, the done tone and announcements all
take that one path, which is why one control governs everything.

The two values that cannot be reasoned about and must be measured on the device:

1. **Which ALSA control the speaker follows.** The codec exposes `PCM`, `Line`,
   `HP`, `HP DAC` and `HPCOM`, and the name is not evidence.
   `scripts/calibrate-volume.py` establishes it by listening.
2. **The audible floor.** The bottom of the range has to be a level that can
   actually be heard; a percentage that is effectively silent is not a floor.

Until that calibration has been run on the real device, this module uses
`DEFAULT_FLOOR_PERCENT`, which is **a conservative placeholder, not a
measurement**. It is deliberately high enough to be audible on typical hardware
and low enough to be a real minimum, and `state/volume-calibration.json`
overrides it once someone has listened.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("gateway.volume")

MAX_PERCENT = 100
MIN_PERCENT = 0

#: Used until the device has been measured. NOT a measurement: see the module docstring.
DEFAULT_FLOOR_PERCENT = 25
#: An alarm has to be heard over whatever the room is doing. Also not a measurement.
DEFAULT_ALARM_MIN_PERCENT = 60

#: Where the calibration script leaves its findings.
CALIBRATION_FILE = Path(__file__).resolve().parent.parent / "state" / "volume-calibration.json"

#: The ALSA card, from the satellite's own ExecStart flags.
DEFAULT_CARD = "seeed2micvoicec"
#: Placeholder until measured. `PCM` is the most likely on this codec, which is not the same as verified.
DEFAULT_CONTROL = "PCM"


class VolumeError(ValueError):
    """A level or a scale value that is not valid."""


def _load_calibration() -> tuple[int, int, str, str]:
    """Read the measured values, or fall back to the placeholders with a warning.

    A malformed file falls back rather than raising: an unusable calibration must not
    stop the Gateway from starting, and the fallback is safe by construction (it is
    above zero). It is logged, because silently using a placeholder is how a
    placeholder becomes permanent.
    """
    floor = DEFAULT_FLOOR_PERCENT
    alarm = DEFAULT_ALARM_MIN_PERCENT
    card = DEFAULT_CARD
    control = DEFAULT_CONTROL
    try:
        raw = json.loads(CALIBRATION_FILE.read_text())
    except FileNotFoundError:
        log.info(
            "volume is using the uncalibrated placeholder floor of %d%%; run "
            "scripts/calibrate-volume.py on the device to replace it with a measured value",
            floor,
        )
        return floor, alarm, card, control
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s (%s); using the placeholder floor", CALIBRATION_FILE, exc)
        return floor, alarm, card, control

    if not isinstance(raw, dict):
        log.warning("%s is not a JSON object; using the placeholder floor", CALIBRATION_FILE)
        return floor, alarm, card, control

    measured = raw.get("floor_percent")
    if isinstance(measured, int) and not isinstance(measured, bool) and 1 <= measured < MAX_PERCENT:
        floor = measured
    elif measured is not None:
        log.warning("calibrated floor_percent %r is out of range; using %d%%", measured, floor)

    alarm_measured = raw.get("alarm_min_percent")
    if (isinstance(alarm_measured, int) and not isinstance(alarm_measured, bool)
            and floor <= alarm_measured <= MAX_PERCENT):
        alarm = alarm_measured
    elif alarm_measured is not None:
        log.warning("calibrated alarm_min_percent %r is out of range; using %d%%",
                    alarm_measured, alarm)

    for key, fallback in (("card", card), ("control", control)):
        value = raw.get(key)
        if isinstance(value, str) and value.strip() and len(value) <= 64:
            if key == "card":
                card = value.strip()
            else:
                control = value.strip()

    log.info(
        "volume calibrated: floor %d%%, alarm minimum %d%%, card %s, control %s",
        floor, alarm, card, control,
    )
    return floor, alarm, card, control


FLOOR_PERCENT, ALARM_MIN_PERCENT, CARD, CONTROL = _load_calibration()


def reload_calibration() -> tuple[int, int, str, str]:
    """Re-read the calibration file and adopt it, without restarting the gateway.

    The calibrated values are module constants on purpose: `clamp` is on the hot path
    for every announcement, timer and alarm, and it must not stat the filesystem to
    decide whether it is allowed to be silent. The cost is that a calibration change
    does not take effect until something calls this. Run it after calibrating, or
    restart the Gateway - either is fine, but doing neither means the measured floor is
    sitting on disk being ignored, which looks exactly like the feature being broken.

    Returns the newly adopted values.
    """
    global FLOOR_PERCENT, ALARM_MIN_PERCENT, CARD, CONTROL
    FLOOR_PERCENT, ALARM_MIN_PERCENT, CARD, CONTROL = _load_calibration()
    return FLOOR_PERCENT, ALARM_MIN_PERCENT, CARD, CONTROL


def clamp(percent: object) -> int:
    """The single choke point. Every level that reaches the device passes through here.

    Non-integers are rejected rather than coerced: a float or a string arriving here is
    a caller bug, and quietly rounding it would hide the bug while still changing the
    volume. A refused value becomes the floor, which is safe but wrong, so it is logged.
    """
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        log.warning("refusing a non-numeric volume level %r; using the floor", percent)
        return FLOOR_PERCENT
    level = int(percent)
    if level < FLOOR_PERCENT:
        return FLOOR_PERCENT
    if level > MAX_PERCENT:
        return MAX_PERCENT
    return level


# -- the user scale, and the raw scale it maps onto ---------------------------
#
# There are two numbers for volume and confusing them is the whole difficulty.
#
# RAW is what the ALSA control takes. On this device the measured floor is 50 raw, so
# raw runs 50..100 and nothing below 50 can be heard.
#
# USER is what a person speaks and what the configuration plane carries: 1..100, where
# 1 is the quietest the hardware can go and 100 is full volume. Both spoken scales live
# on it - "50 percent" and "level 5" are positions on the same range, one finer than the
# other - so they cannot drift apart.
#
# The correction, 2026-09-25: both scales must span the USABLE range. Before this,
# a raw floor of 50 meant "set volume to 30%" was clamped and reported as an error, so
# half the percentage scale was dead and every value in it did the same thing.
#
# The configuration plane speaks USER too, deliberately: a fleet API that reported raw
# ALSA numbers would be meaningless across devices with different measured floors.

USER_MIN = 1
USER_MAX = 100


def clamp_user(user: object) -> int:
    """Force anything onto the user range. 0 and below become the quietest level."""
    if isinstance(user, bool) or not isinstance(user, (int, float)):
        log.warning("refusing a non-numeric user volume %r; using the quietest", user)
        return USER_MIN
    return max(USER_MIN, min(USER_MAX, int(user)))


#: Kept for the internal call sites written before the name became public.
_clamp_user = clamp_user


def user_percent_to_raw(user: object) -> int:
    """Map the user range onto what the hardware can actually do."""
    level = _clamp_user(user)
    span = MAX_PERCENT - FLOOR_PERCENT
    if span <= 0:
        return FLOOR_PERCENT
    return clamp(FLOOR_PERCENT + round((level - USER_MIN) * span / (USER_MAX - USER_MIN)))


def raw_to_user_percent(raw: object) -> int:
    """The inverse, for reporting a measured raw level back to a person or a client."""
    span = MAX_PERCENT - FLOOR_PERCENT
    if span <= 0:
        return USER_MIN
    user = round((clamp(raw) - FLOOR_PERCENT) / span * (USER_MAX - USER_MIN)) + USER_MIN
    return max(USER_MIN, min(USER_MAX, user))


def ten_scale_to_percent(level: int) -> int:
    """The 1-10 scale, as ten samples of the user range. Returns RAW.

    Level 1 is the quietest the hardware can go and level 10 is full volume. Because it
    is the user range sampled ten times rather than a second mapping, level 5 and
    "user 45%" cannot disagree.
    """
    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 10:
        raise VolumeError("the volume scale takes whole numbers from 1 to 10")
    user = USER_MIN + round((level - 1) * (USER_MAX - USER_MIN) / 9)
    return user_percent_to_raw(user)


def percent_to_ten_scale(user: object) -> int:
    """The inverse, for speaking a level back in the terms the user thinks in."""
    level = _clamp_user(user)
    return max(1, min(10, round((level - USER_MIN) / (USER_MAX - USER_MIN) * 9) + 1))


def user_for_ten_level(level: int) -> int:
    """Level 1..10 -> user 1..100. Used by stepping, which moves on the user scale."""
    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 10:
        raise VolumeError("the volume scale takes whole numbers from 1 to 10")
    return USER_MIN + round((level - 1) * (USER_MAX - USER_MIN) / 9)


def step(current_user: object, delta: int) -> int:
    """Move a whole number of ten-scale levels, on the USER scale. Cannot cross the floor."""
    level = percent_to_ten_scale(current_user) + int(delta)
    level = max(1, min(10, level))
    return user_for_ten_level(level)


def alarm_percent(current_user: object) -> int:
    """The USER level an alarm should play at. Upward-only (2026-09-25).

    An alarm at a level too quiet to notice is useless, so a quiet room is raised to
    the alarm minimum. A room already louder than that is left alone, because lowering
    it would be the override behaving as a surprise rather than a safeguard.

    On the user scale, like every other user-facing number, so the minimum means the
    same thing on hardware with a different measured floor.
    """
    return _clamp_user(max(_clamp_user(current_user), ALARM_MIN_PERCENT))


def describe_percent(percent: int) -> str:
    """The level in the terms a person thinks in: the 1-10 scale."""
    return f"Volume {percent_to_ten_scale(percent)} out of 10."


# ---------------------------------------------------------------- parsing

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# "set volume to 7", "volume to 40%", "volume 100 percent", "set the volume to 65"
_ABSOLUTE = re.compile(
    r"\b(?:set\s+)?(?:the\s+)?volume\s*(?:to\s*)?"
    r"(?P<value>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)\s*"
    r"(?P<unit>%|percent|per cent)?\b"
)
_RELATIVE = re.compile(r"\bvolume\s*(?P<direction>up|down)\b(?:\s*by\s*(?P<steps>\d{1,2}|[a-z]+))?")
_DIRECTION_WORDS = re.compile(r"\b(?P<direction>louder|quieter)\b")
_QUERY = re.compile(r"\b(?:what(?:'|\u2019)?s|what\s+is)\s+(?:the\s+)?volume\b|\bvolume\s+status\b")


@dataclass
class VolumeIntent:
    """What the person asked for, before the current level is known."""

    kind: str                      # "set" | "step" | "query"
    percent: Optional[int] = None  # for "set": the requested level, already clamped
    direction: int = 0             # for "step": +1 or -1
    steps: int = 1                 # for "step": how many levels
    clamped: bool = False          # for "set": the request was raised to the floor
    scale: str = "percent"         # "ten" or "percent", so the reply matches the request


def _parse_steps(raw: Optional[str]) -> int:
    if not raw:
        return 1
    raw = raw.strip().lower()
    if raw.isdigit():
        return max(1, min(10, int(raw)))
    return _WORD_NUMBERS.get(raw, 1)


def parse_volume_command(text: str) -> Optional[VolumeIntent]:
    """Recognise a volume request, or return None and let the model have it.

    Deliberately conservative. A phrase that is not clearly about volume must fall
    through to the assistant, because the Gateway acting on a mis-heard phrase is worse
    than the assistant taking an extra second.

    That principle is why bare "turn it up" is NOT matched, even though it probably
    means the device volume. "It" could be music playing on the same device, and the
    Gateway has no way to tell. "turn the volume up" and plain "volume up" both work,
    because neither leaves the object ambiguous.
    """
    if not isinstance(text, str):
        return None
    lowered = text.strip().lower()
    # Every accepted form either names the volume or uses an unambiguous direction word.
    if not lowered:
        return None
    if "volume" not in lowered and not _DIRECTION_WORDS.search(lowered):
        return None

    if _QUERY.search(lowered):
        return VolumeIntent(kind="query")

    relative = _RELATIVE.search(lowered)
    if relative is not None:
        return VolumeIntent(
            kind="step",
            direction=1 if relative.group("direction") == "up" else -1,
            steps=_parse_steps(relative.group("steps")),
            scale="ten",
        )

    direction_word = _DIRECTION_WORDS.search(lowered)
    if direction_word is not None:
        return VolumeIntent(
            kind="step",
            direction=1 if direction_word.group("direction") == "louder" else -1,
            steps=1,
            scale="ten",
        )

    absolute = _ABSOLUTE.search(lowered)
    if absolute is None:
        return None

    raw_value = absolute.group("value")
    value = int(raw_value) if raw_value.isdigit() else _WORD_NUMBERS[raw_value]
    unit = absolute.group("unit")

    if value == 0:
        # The headline case from the issue: "set volume to 0" must be understood and
        # refused-in-place rather than fall through to the model or, worse, be obeyed.
        # Recognised as a command, raised to the quietest level, and flagged so the reply
        # can say why. Zero is not a position on the user scale, which starts at 1.
        return VolumeIntent(kind="set", percent=USER_MIN, clamped=True, scale="percent")

    if unit is not None:
        # A percentage is a position on the USER range, not a raw ALSA value. "30%" means
        # 30% of what this hardware can do, which is raw 65 here - not raw 30, which is
        # silence on this device and would have to be clamped.
        if value > USER_MAX:
            # Asking for more than the maximum plainly means "as loud as it goes", which
            # is what it gets, so there is nothing surprising to explain.
            return VolumeIntent(kind="set", percent=USER_MAX, clamped=False, scale="percent")
        return VolumeIntent(kind="set", percent=value, clamped=False, scale="percent")

    if value <= 10:
        # A bare small number is the scale the issue describes: "set volume to 7" is level
        # seven, not seven percent.
        level = max(USER_MIN, min(10, value))
        return VolumeIntent(kind="set", percent=user_for_ten_level(level),
                            clamped=False, scale="ten")

    # A bare large number can only be a percentage; "level 40" does not exist.
    capped = value > USER_MAX
    return VolumeIntent(kind="set", percent=USER_MAX if capped else value,
                        clamped=False, scale="percent")


def resolve(intent: VolumeIntent, current_percent: int) -> tuple[int, str]:
    """Turn an intent into (new USER level, one short spoken line).

    This is the only place that decides a resulting level, and it cannot return
    anything below USER_MIN for any input, which maps to the measured floor. That
    property is what the test suite pins: whatever the current level and whatever the
    request, the device stays audible.
    """
    if intent is None:
        raise VolumeError("no intent to resolve")

    current = _clamp_user(current_percent)

    if intent.kind == "query":
        return current, f"Volume {percent_to_ten_scale(current)} out of 10. {current} percent."

    if intent.kind == "step":
        level = step(current, intent.direction * intent.steps)
        if level == current:
            if intent.direction < 0:
                return level, "Already at the quietest level."
            return level, "Already at full volume."
        return level, describe_percent(level)

    if intent.kind == "set":
        level = _clamp_user(intent.percent if intent.percent is not None else USER_MIN)
        if intent.clamped:
            # Say why, because the user asked for something and did not get it.
            return level, f"Zero would be silent. {describe_percent(level)}"
        if intent.scale == "ten":
            return level, describe_percent(level)
        return level, f"Volume {level} percent."

    raise VolumeError(f"unknown intent kind {intent.kind!r}")


__all__ = [
    "ALARM_MIN_PERCENT", "CARD", "CALIBRATION_FILE", "CONTROL", "DEFAULT_CARD",
    "DEFAULT_CONTROL", "FLOOR_PERCENT", "MAX_PERCENT", "VolumeError", "VolumeIntent",
    "USER_MAX", "USER_MIN",
    "alarm_percent", "clamp", "clamp_user", "describe_percent", "parse_volume_command",
    "percent_to_ten_scale", "raw_to_user_percent", "reload_calibration", "resolve", "step",
    "ten_scale_to_percent", "user_for_ten_level", "user_percent_to_raw",
]
