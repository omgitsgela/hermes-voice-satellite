#!/usr/bin/env python3
"""Device-side apply adapter checks (issue #10 / #6).

The adapter is what turns desired settings into hardware change. The property that
matters most here is honesty: it must never report a setting as applied when it was not,
and it must never report a level it did not set.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pi.device_apply import make_apply_adapter  # noqa: E402
from pi.volume_control import AlsaVolume, VolumeCommandError  # noqa: E402

problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


class FakeAmixer:
    def __init__(self, fail_set: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail_set = fail_set

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        r = R()
        if self.fail_set and "sset" in argv:
            r.returncode = 1
            r.stderr = "amixer: unable to set"
        return r


# ---------------------------------------------------------------- volume applies

fake = FakeAmixer()
adapter = make_apply_adapter(AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake))
actual, health, error = adapter({"playback_volume": 70})
check("a valid volume is applied", actual.get("playback_volume") == 70, actual)
check("applying a valid volume is healthy", health == "healthy", health)
check("applying a valid volume reports no problem", error is None, error)
check("the device was actually told", any("sset" in c for c in fake.calls), fake.calls)
check("the level was persisted for reboot survival",
      any("alsactl" in c[0] for c in fake.calls), fake.calls)

# ---------------------------------------------------------------- the floor is surfaced

fake = FakeAmixer()
adapter = make_apply_adapter(AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake))
actual, health, error = adapter({"playback_volume": 0})
# The setting is on the USER scale now, so zero is refused up to user 1 - the quietest
# this hardware can be - and user 1 maps to the raw floor of 25 on this test device.
check("a zero request is reported as the quietest USER level",
      actual.get("playback_volume") == 1, actual)
check("a zero request reaches amixer as the raw floor, never 0%",
      any("25%" in part for c in fake.calls for part in c)
      and not any("0%" in part for c in fake.calls for part in c), fake.calls)
check("the floor adjustment is reported rather than silent",
      error is not None and "raised" in error, error)
check("the report says what it was raised from and to, on the user scale",
      "from 0 to 1" in error, error)
check("the adjusted level is still healthy, because it was applied",
      health == "healthy", health)

# ---------------------------------------------------------------- the user range maps

# The whole point of the user scale: the middle of it is the middle of what the hardware
# can do, not the middle of the raw range. On a floor of 25 the usable range is 25..100,
# so user 50 must land near raw 63 - NOT raw 50, which would be almost the bottom.
fake_mid = FakeAmixer()
adapter_mid = make_apply_adapter(
    AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake_mid))
actual_mid, _h, _e = adapter_mid({"playback_volume": 50})
sent = [part for c in fake_mid.calls for part in c if part.endswith("%")]
check("user 50% maps into the middle of the USABLE range, not the middle of raw",
      bool(sent) and 60 <= int(sent[0].rstrip("%")) <= 65, sent)
check("user 50% is reported back as 50%", actual_mid.get("playback_volume") == 50, actual_mid)

fake_top = FakeAmixer()
adapter_top = make_apply_adapter(
    AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake_top))
actual_top, _h, _e = adapter_top({"playback_volume": 100})
sent_top = [part for c in fake_top.calls for part in c if part.endswith("%")]
check("user 100% reaches full raw volume", sent_top and sent_top[0] == "100%", sent_top)
check("user 100% is reported back as 100%", actual_top.get("playback_volume") == 100, actual_top)

# ---------------------------------------------------------------- a failure is not a success

fake = FakeAmixer(fail_set=True)
adapter = make_apply_adapter(AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake))
actual, health, error = adapter({"playback_volume": 60})
check("a failed set reports no applied level", "playback_volume" not in actual, actual)
check("a failed set is not healthy", health != "healthy", health)
check("a failed set explains itself", error is not None and "playback_volume" in error, error)

# ---------------------------------------------------------------- unimplemented settings

fake = FakeAmixer()
adapter = make_apply_adapter(AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake))
actual, health, error = adapter({"mic_gain": 78})
check("a setting with no adapter yet is not reported as applied", actual == {}, actual)
check("a setting with no adapter yet is degraded, not healthy", health == "degraded", health)
check("the unimplemented setting is named", error is not None and "mic_gain" in error, error)
check("no amixer command was issued for an unimplemented setting", fake.calls == [], fake.calls)

# A mix: the volume applies, the rest is named as staged.
fake = FakeAmixer()
adapter = make_apply_adapter(AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake))
actual, health, error = adapter({"playback_volume": 80, "led_brightness": 40})
check("a mixed request applies what it can", actual.get("playback_volume") == 80, actual)
check("a mixed request is still degraded, because part was staged",
      health == "degraded", health)
check("a mixed request names the staged setting",
      error is not None and "led_brightness" in error, error)

# ---------------------------------------------------------------- the agent end-to-end

import asyncio  # noqa: E402

from wyoming.event import Event  # noqa: E402

from pi.config_agent import DeviceConfigAgent  # noqa: E402


async def agent_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fake = FakeAmixer()
        agent = DeviceConfigAgent(
            pathlib.Path(tmp) / "config.json",
            apply=make_apply_adapter(
                AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake)),
            device_id="kitchen-1",
        )
        report = await agent.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1,
            "device_id": "kitchen-1",
            "generation": 1,
            "settings": {"playback_volume": 55},
        }))
        check("the agent reports a healthy volume change",
              report.data["health"] == "healthy", report.data)
        check("the agent reports the applied level, not the requested one",
              report.data["actual"].get("playback_volume") == 55, report.data)

        # And a zero request through the whole chain still lands audible.
        fake2 = FakeAmixer()
        agent2 = DeviceConfigAgent(
            pathlib.Path(tmp) / "config2.json",
            apply=make_apply_adapter(
                AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake2)),
            device_id="kitchen-1",
        )
        report = await agent2.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1,
            "device_id": "kitchen-1",
            "generation": 1,
            "settings": {"playback_volume": 0},
        }))
        # On the user scale a zero request is refused up to user 1; the raw floor is what
        # user 1 maps to, and that is what amixer must receive.
        check("zero through the whole chain lands on the quietest user level",
              report.data["actual"].get("playback_volume") == 1, report.data)
        check("zero through the whole chain reaches amixer as the raw floor",
              any("25%" in part for c in fake2.calls for part in c), fake2.calls)
        check("zero never reaches amixer as 0%",
              not any("0%" in part for c in fake2.calls for part in c), fake2.calls)


asyncio.run(agent_round_trip())

print(f"device apply checks: {checks - len(problems)}/{checks} passed")
for problem in problems:
    print(f"  FAIL: {problem}")
raise SystemExit(1 if problems else 0)
