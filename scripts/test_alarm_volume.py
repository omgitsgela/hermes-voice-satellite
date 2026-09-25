#!/usr/bin/env python3
"""Alarm volume override checks (issue #10, Angela's decision 2026-09-25).

Her decision: **an alarm may override upward to a minimum alarm level, so an alarm is
always audible.** It must never override downward, because lowering a level she chose
would be the override behaving as a surprise rather than a safeguard.

The last acceptance criterion in #10 is that no code path can take the device to
silence, "including announcements and timers". This pins the alarm path.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import wave

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge.announce import Announcement, Announcer  # noqa: E402
from bridge.volume import (  # noqa: E402
    ALARM_MIN_PERCENT,
    FLOOR_PERCENT,
    MAX_PERCENT,
    USER_MIN,
)

problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


class Sat:
    def __init__(self) -> None:
        self.played: list[str] = []

    async def play_wav(self, path, **_kwargs):
        self.played.append(str(path))
        return True


def make_wav(path: pathlib.Path, seconds: float) -> pathlib.Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(22050)
        handle.writeframes(b"\x00" * int(22050 * 2 * seconds))
    return path


class Harness:
    """An Announcer with the volume callbacks wired to a recorded history."""

    def __init__(self, tmp: str, *, start_level: int = 50, set_ok: bool = True) -> None:
        self.level = start_level
        self.history: list[int] = []
        self.set_ok = set_ok
        self.sat = Sat()
        self.announcer = Announcer(
            sat=self.sat,
            turn_started_at=lambda: None,
            timer_state_path=pathlib.Path(tmp) / "timers.json",
        )
        self.announcer._volume_get = lambda: self.level
        self.announcer._volume_set = self._set

    async def _set(self, level: int) -> bool:
        self.history.append(level)
        if self.set_ok:
            self.level = level
        return self.set_ok


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        speech = make_wav(pathlib.Path(tmp) / "speech.wav", 0.1)
        tone = make_wav(pathlib.Path(tmp) / "alarm-tone.wav", 0.05)

        # ---------------------------------------------------- an alarm raises a quiet room
        # Start at the QUIETEST USER LEVEL, which is what an alarm has to raise. Starting at
        # the raw floor no longer exercises anything: alarm_minimum is on the user scale, and
        # the raw floor maps to user 1, so a room at user 50 is already at the minimum.
        h = Harness(tmp, start_level=USER_MIN)
        item = Announcement(id="a1", text="x", source="alarm", key="k", sound="alarm",
                            received_at=0.0)
        h.announcer._build_wav = lambda _item: speech
        h.announcer._volume_hold_seconds = lambda _p: 0.0  # keep the check fast

        await h.announcer._announce(item)
        # Assert on the history safely. Indexing it directly made this suite CRASH rather
        # than fail when the override was broken, which stopped every later check in the
        # file from running - so a single breakage looked like a shorter, quieter pass.
        first_set = h.history[0] if h.history else None
        last_set = h.history[-1] if h.history else None
        check("an alarm raises a room set to the quietest level",
              first_set == max(ALARM_MIN_PERCENT, USER_MIN), h.history)
        check("the raised level is above the quietest, so it is actually audible",
              first_set is not None and first_set > USER_MIN, h.history)
        check("the alarm level is restored afterwards", last_set == USER_MIN, h.history)
        check("the alarm still played", len(h.sat.played) == 1, h.sat.played)

        # ---------------------------------------------------- it never lowers a loud room
        h = Harness(tmp, start_level=MAX_PERCENT)
        h.announcer._build_wav = lambda _item: speech
        h.announcer._volume_hold_seconds = lambda _p: 0.0
        await h.announcer._announce(item)
        check("an alarm never lowers a level the user chose louder",
              all(level >= MAX_PERCENT for level in h.history), h.history)
        check("an alarm at full volume issues no volume change at all",
              h.history == [], h.history)

        # A level already above the alarm minimum is left alone.
        h = Harness(tmp, start_level=min(MAX_PERCENT, ALARM_MIN_PERCENT + 10))
        h.announcer._build_wav = lambda _item: speech
        h.announcer._volume_hold_seconds = lambda _p: 0.0
        await h.announcer._announce(item)
        check("an alarm above the minimum is not touched", h.history == [], h.history)

        # ---------------------------------------------------- non-alarm sounds are untouched
        for sound in ("chime", "timer", "none"):
            h = Harness(tmp, start_level=FLOOR_PERCENT)
            h.announcer._build_wav = lambda _item: speech
            h.announcer._volume_hold_seconds = lambda _p: 0.0
            other = Announcement(id="a2", text="x", source="hubitat", key=None, sound=sound,
                                 received_at=0.0)
            await h.announcer._announce(other)
            check(f"a {sound!r} announcement does not change the volume", h.history == [], h.history)

        # ---------------------------------------------------- no callbacks wired: no crash
        plain = Announcer(
            sat=Sat(),
            turn_started_at=lambda: None,
            timer_state_path=pathlib.Path(tmp) / "timers.json",
        )
        plain._build_wav = lambda _item: speech
        plain._volume_hold_seconds = lambda _p: 0.0
        try:
            await plain._announce(item)
            check("an alarm without volume callbacks still plays", True)
        except Exception as exc:  # noqa: BLE001
            check("an alarm without volume callbacks still plays", False,
                  f"{type(exc).__name__}: {exc}")

        # ---------------------------------------------------- a failed raise still plays
        h = Harness(tmp, start_level=FLOOR_PERCENT, set_ok=False)
        h.announcer._build_wav = lambda _item: speech
        h.announcer._volume_hold_seconds = lambda _p: 0.0
        try:
            await h.announcer._announce(item)
            check("an alarm still plays when the volume cannot be raised", True)
        except Exception as exc:  # noqa: BLE001
            check("an alarm still plays when the volume cannot be raised", False,
                  f"{type(exc).__name__}: {exc}")
        # Guarded, for the same reason as above: a direct index crashes the suite under
        # sabotage and silently skips every check after it.
        check("a failed raise does not leave the level tracked as changed",
              not h.history
              or h.history[-1] in (FLOOR_PERCENT, max(ALARM_MIN_PERCENT, FLOOR_PERCENT)),
              h.history)

        # ---------------------------------------------------- the hold duration is real
        # The raised level has to last as long as the alarm is audible, or the override is
        # cosmetic: play_wav returns when the clip is SENT, not when it finishes.
        # The file must actually exist before measuring it: a missing file takes the
        # fallback path, which would let these checks pass while testing nothing.
        long_clip = make_wav(pathlib.Path(tmp) / "long.wav", 2.0)
        check("the measured clip exists, so the measurement is real",
              long_clip.is_file(), long_clip)
        held = Announcer._volume_hold_seconds(long_clip)
        check("the hold is at least as long as the clip", held >= 2.0, held)
        check("the hold includes a margin for the device's own buffering", held > 2.0, held)
        check("the hold is bounded, so a long clip cannot pin the volume",
              held < 600, held)

        # A shorter clip must hold for less time, proving the duration is measured rather
        # than a constant that merely happens to be large enough.
        short_clip = make_wav(pathlib.Path(tmp) / "short.wav", 0.25)
        short_held = Announcer._volume_hold_seconds(short_clip)
        check("a shorter clip is held for less time", short_held < held, (short_held, held))
        check("a short clip is still held long enough to be heard", short_held >= 0.25, short_held)

        # And an unmeasurable file falls back rather than raising or returning zero.
        missing = pathlib.Path(tmp) / "does-not-exist.wav"
        fallback = Announcer._volume_hold_seconds(missing)
        check("an unmeasurable clip falls back to a bounded hold rather than failing",
              0 < fallback < 600, fallback)

    print(f"alarm volume checks: {checks - len(problems)}/{checks} passed")
    for problem in problems:
        print(f"  FAIL: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
