#!/usr/bin/env python3
"""An alarm must be audible on EVERY satellite it plays on (regression, found 2026-10-29).

An announcement fans out to every armed satellite, but the alarm volume override read and wrote
through the loop's SINGLE volume callbacks -- which address one device. With two satellites that
meant: the addressed device was raised to the alarm minimum, and every other device played the alarm
at whatever level it happened to be sitting at. Measured the day this was found: one satellite was at
1%, so an alarm in that room was inaudible while the other speaker was correctly raised to the
minimum. An alarm nobody can hear is not an alarm.

What this pins:
1. Two devices at DIFFERENT levels are both raised, and each is restored to its OWN previous value
   (not to one shared value).
2. A device whose raise fails does not stop the others from being raised and restored.
3. A room already above the alarm minimum is left alone entirely.
4. A non-alarm announcement never touches any volume.
5. A single-device deployment (no per-device callbacks) keeps the old behaviour exactly.

Run with the bridge venv:

    .venv/bin/python scripts/test_announce_alarm_volume.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge.announce import Announcement, Announcer  # noqa: E402
from bridge.volume import alarm_percent  # noqa: E402

checks = 0
failures: list[str] = []


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


class FakeSat:
    """A satellite that records the clips it was asked to play."""

    def __init__(self, device_id: str, *, fail: bool = False):
        self.device_id = device_id
        self.played: list[str] = []
        self.fail = fail

    async def play_wav(self, wav, wait_for_played: bool = False):
        if self.fail:
            raise RuntimeError("device gone")
        self.played.append(str(wav))


def build(*, sats: list[FakeSat], levels: dict[str, int],
          failing_raises: set[str] | None = None,
          per_device: bool = True):
    """An announcer whose volume path is recordable, with the given fleet and levels."""
    levels = dict(levels)
    raise_calls: list[tuple[str | None, int]] = []
    failing_raises = failing_raises or set()

    def get_for(device_id=None):
        if device_id is None:
            return min(levels.values()) if levels else 50
        return levels[device_id]

    async def set_for(level, device_id=None):
        key = device_id if device_id is not None else (next(iter(levels)) if levels else None)
        raise_calls.append((key, level))
        if key in failing_raises and level > levels.get(key, 0):
            return False
        if key is not None:
            levels[key] = level
        return True

    kwargs: dict[str, Any] = dict(
        sat=sats[0] if sats else None,
        turn_started_at=lambda: None,
        satellites=lambda: list(sats),
        config_plane=None,
    )
    if per_device:
        kwargs["volume_get_for"] = get_for
        kwargs["volume_set_for"] = set_for
    else:
        # Legacy shape: one callback pair, no device argument -- the single-device deployment.
        kwargs["volume_get"] = lambda: get_for(None)
        kwargs["volume_set"] = lambda level: set_for(level, None)

    announcer = Announcer(**kwargs)
    announcer._wait_for_quiet = _no_wait
    announcer._build_wav = lambda item: pathlib.Path("/tmp/fake-alarm.wav")
    announcer._volume_hold_seconds = lambda wav: 0.0
    return announcer, levels, raise_calls


async def _no_wait(item):
    return 0.0


def item(sound: str = "alarm") -> Announcement:
    return Announcement(id="t1", text="fire alarm", source="test", key=None,
                        sound=sound, received_at=time.time())


async def main() -> int:
    # 1) two devices at different levels, both raised, each restored to its own value
    a, b = FakeSat("alpha"), FakeSat("beta")
    announcer, levels, calls = build(sats=[a, b], levels={"alpha": 20, "beta": 1})
    before = dict(levels)
    await announcer._announce(item())
    raised_a = alarm_percent(before["alpha"])
    raised_b = alarm_percent(before["beta"])
    check("alpha was raised to the alarm minimum", calls[0], ("alpha", raised_a))
    check("beta was raised to the alarm minimum", calls[1], ("beta", raised_b))
    check("both clips played", (len(a.played), len(b.played)), (1, 1))
    check("alpha restored to ITS OWN value", levels["alpha"], before["alpha"])
    check("beta restored to ITS OWN value", levels["beta"], before["beta"])

    # 2) one device refusing the raise must not stop the other
    c, d = FakeSat("gamma"), FakeSat("delta")
    announcer, levels, calls = build(sats=[c, d], levels={"gamma": 1, "delta": 1},
                                     failing_raises={"gamma"})
    await announcer._announce(item())
    check("both devices still received the clip", (len(c.played), len(d.played)), (1, 1))
    check("the other device was still raised", any(k == "delta" for k, _ in calls), True)
    check("and restored", levels["delta"], 1)

    # 3) a room already louder than the minimum is untouched
    e = FakeSat("epsilon")
    announcer, levels, calls = build(sats=[e], levels={"epsilon": 95})
    await announcer._announce(item())
    check("a loud room is not touched at all", calls, [])
    check("and still hears the alarm", len(e.played), 1)

    # 4) a non-alarm announcement never moves a volume
    f = FakeSat("zeta")
    announcer, levels, calls = build(sats=[f], levels={"zeta": 2})
    await announcer._announce(item(sound="chime"))
    check("a non-alarm leaves the level alone", calls, [])

    # 5) single-device deployment keeps the legacy behaviour
    g = FakeSat("eta")
    announcer, levels, calls = build(sats=[g], levels={"eta": 3}, per_device=False)
    await announcer._announce(item())
    check("the legacy path still raises and restores", levels["eta"], 3)
    check("and played the clip", len(g.played), 1)

    print(f"alarm volume checks: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
