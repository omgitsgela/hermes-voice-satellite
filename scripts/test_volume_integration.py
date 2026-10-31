#!/usr/bin/env python3
"""Bridge-side volume integration checks (issue #10).

Covers the part that ties the parser to the device: a spoken request must change the
device's level, the Gateway must never claim a change it did not make, and there must be
no path from any spoken request to silence.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gateway.config_plane import ConfigPlane  # noqa: E402
from gateway.loop import VoiceLoop  # noqa: E402
# NOTE: `loop.volume` is a USER value (1..100), not a raw ALSA one. Comparing it against
# FLOOR_PERCENT/MAX_PERCENT was comparing two different scales - it passed only because
# the raw floor of 50 happens to be a legal user value and the assertions were >= rather
# than ==. That kind of pass is worse than a failure, so the user bounds are used now.
from gateway.volume import USER_MAX, USER_MIN  # noqa: E402


class Sat:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def play_wav(self, *_a, **_k):
        pass

    async def send(self, _event):
        return True


def make_loop(tmp: str, *, dispatch_ok: bool = True):
    """A VoiceLoop with just the state the volume path needs."""
    loop = VoiceLoop.__new__(VoiceLoop)
    loop.config_plane = ConfigPlane(pathlib.Path(tmp) / "fleet.json")
    loop.device_id = "kitchen-1"
    loop.volume = 50
    loop.dispatched: list[tuple[str, int]] = []

    async def dispatch(device_id: str, record: dict) -> bool:
        loop.dispatched.append((device_id, record["desired"]["playback_volume"]))
        return dispatch_ok

    loop._dispatch_device_config = dispatch
    return loop


async def main() -> int:
    problems: list[str] = []
    checks = 0

    def check(label: str, condition: bool, detail="") -> None:
        nonlocal checks
        checks += 1
        if not condition:
            problems.append(f"{label}: {detail}")

    with tempfile.TemporaryDirectory() as tmp:
        # ---------------------------------------------- an absolute request
        loop = make_loop(tmp)
        reply = await loop._volume_command("set volume to 7")
        check("a spoken set returns something to say", bool(reply), reply)
        check("the level changed", loop.volume != 50, loop.volume)
        check("the device was told", loop.dispatched and loop.dispatched[-1][1] == loop.volume,
              loop.dispatched)
        check("the reply states the resulting level",
              str(loop.volume) in reply or "out of" in reply, reply)

        # ---------------------------------------------- a relative request
        before = loop.volume
        reply = await loop._volume_command("volume up")
        check("a step moves up from the current level", loop.volume > before, loop.volume)
        check("the device was told about the step",
              loop.dispatched[-1][1] == loop.volume, loop.dispatched)

        # ---------------------------------------------- the query
        before = loop.volume
        count = len(loop.dispatched)
        reply = await loop._volume_command("what's the volume")
        check("a query changes nothing", loop.volume == before, loop.volume)
        check("a query does not talk to the device", len(loop.dispatched) == count, loop.dispatched)
        check("a query answers with the current level",
              str(loop.volume) in reply, reply)

        # ---------------------------------------------- the headline case
        loop = make_loop(tmp)
        reply = await loop._volume_command("set volume to 0")
        check("'set volume to 0' does not silence the device",
              loop.volume >= USER_MIN, loop.volume)
        check("'set volume to 0' tells the device a level above zero",
              loop.dispatched[-1][1] >= USER_MIN, loop.dispatched)
        check("'set volume to 0' explains itself", 
              "silent" in reply.lower() or "zero" in reply.lower(), reply)

        # ---------------------------------------------- not a volume request
        loop = make_loop(tmp)
        for text in ("turn on follow up mode", "what is the weather", "play some music", ""):
            reply = await loop._volume_command(text)
            check(f"{text!r} is not answered as a volume request", reply == "", reply)
        check("a non-volume utterance never reaches the device", loop.dispatched == [],
              loop.dispatched)

        # ---------------------------------------------- the device refusing
        loop = make_loop(tmp, dispatch_ok=False)
        reply = await loop._volume_command("set volume to 7")
        check("a failed dispatch does not claim success",
              "could" in reply.lower() or "not" in reply.lower(), reply)
        # The level the Gateway tracks is the RECORDED intent, and the sentence says plainly
        # that it has not been applied. So the two together are honest: the durable state and
        # the tracked value agree with each other, and neither claims the device moved.
        recorded = loop.config_plane.get_device("kitchen-1")["desired"]["playback_volume"]
        check("the recorded level and the tracked level agree after a failed dispatch",
              loop.volume == recorded, (loop.volume, recorded))
        check("a failed dispatch is still recorded durably, so it converges later",
              recorded >= USER_MIN, recorded)

        # ---------------------------------------------- no spoken request can silence it
        requests = ["set volume to 0", "set volume to 0%", "volume down", "volume down by 9",
                    "quieter", "set volume to 1%", "volume down by two"]
        for text in requests:
            loop = make_loop(tmp)
            # Start as quiet as the floor allows, the worst case.
            loop.volume = USER_MIN
            await loop._volume_command(text)
            check(f"{text!r} from the quietest stays audible", loop.volume >= USER_MIN, loop.volume)
            if loop.dispatched:
                check(f"{text!r} never sends a silent level to the device",
                      loop.dispatched[-1][1] >= USER_MIN, loop.dispatched)

        # Start loud, step all the way down, and confirm it stops at the floor.
        loop = make_loop(tmp)
        loop.volume = USER_MAX
        for _ in range(30):
            await loop._volume_command("volume down")
        check("stepping down repeatedly stops at the quietest and never below",
              loop.volume == USER_MIN, loop.volume)
        check("every level sent to the device during the sweep was audible",
              all(level >= USER_MIN for _id, level in loop.dispatched), loop.dispatched)

        # ---------------------------------------------- persistence
        loop = make_loop(tmp)
        await loop._volume_command("set volume to 7")
        saved = loop.config_plane.get_device("kitchen-1")
        check("the level is recorded in the durable desired state",
              saved["desired"].get("playback_volume") == loop.volume, saved["desired"])

        # A restart must resume the recorded level, not a default.
        restored = VoiceLoop.__new__(VoiceLoop)
        restored.config_plane = ConfigPlane(pathlib.Path(tmp) / "fleet.json")
        restored.device_id = "kitchen-1"
        loop._load_volume(restored)
        check("the level survives a restart", restored.volume == loop.volume,
              (restored.volume, loop.volume))

        # ---------------------------------------------- a raw stored zero is clamped on load
        #
        # This is the path that actually exercises clamp() with a raw value. The spoken
        # checks above cannot: the parser and the ten-scale mapping each apply the floor
        # independently, so they keep passing even if clamp itself loses its floor. Without
        # this check the sabotage run passed, which is how the blind spot was found.
        hostile = pathlib.Path(tmp) / "hostile.json"
        plane = ConfigPlane(hostile)
        plane.set_desired("kitchen-1", {"playback_volume": 0})
        direct = plane.get_device("kitchen-1")["desired"]["playback_volume"]
        check("the plane itself stores what was written, even a zero",
              direct == 0, direct)

        loader = VoiceLoop.__new__(VoiceLoop)
        loader.config_plane = ConfigPlane(hostile)
        loader.device_id = "kitchen-1"
        loaded = loader._load_volume(loader)
        check("a stored zero is raised to the floor on load, not resumed as silence",
              loaded >= USER_MIN, loaded)
        check("the loop's tracked level is the raised one, not the stored zero",
              loader.volume == loaded, (loader.volume, loaded))

        # And a stored value below a measured floor is raised to it too.
        plane.set_desired("kitchen-1", {"playback_volume": 1})
        loader2 = VoiceLoop.__new__(VoiceLoop)
        loader2.config_plane = ConfigPlane(hostile)
        loader2.device_id = "kitchen-1"
        check("a stored level below the floor is raised on load",
              loader2._load_volume(loader2) >= USER_MIN, loader2.volume)

        # ---------------------------------------------- the loop addresses the real device
        #
        # A spoken volume command must be filed under the id the device registered with. The
        # loop used to hold VOICE_SESSION_ID, which is a SESSION id: the write created a
        # config record for a device that does not exist, the dispatch then found no
        # connection under that name, and the reply told the user it could not reach the
        # device. Every piece looked correctly wired, so nothing caught it.
        loop = make_loop(tmp)
        loop.device_id = ""          # the state before any device has registered
        loop.volume = 0
        adopted = loop._load_volume(loop)
        check("with no device registered the level is the quietest, not a raw default",
              adopted == USER_MIN, adopted)

        loop._adopt_device("kitchen-1")
        check("the loop adopts the id the device registered with",
              loop.device_id == "kitchen-1", loop.device_id)

        await loop._volume_command("set volume to 50%")
        check("after adopting, a spoken command is dispatched to that device",
              bool(loop.dispatched) and loop.dispatched[-1][0] == "kitchen-1",
              loop.dispatched)
        check("and the record exists under the real device id",
              loop.config_plane.get_device("kitchen-1")["desired"].get("playback_volume") == 50,
              loop.config_plane.get_device("kitchen-1")["desired"])

        # Adopting the same device twice is a no-op, and adopting a different one re-reads
        # that device's own stored level rather than keeping the previous device's.
        before_volume = loop.volume
        loop._adopt_device("kitchen-1")
        check("re-adopting the same device changes nothing", loop.volume == before_volume,
              loop.volume)

        # The WIRING, not just the helper. Calling _adopt_device directly passes even when
        # nothing calls it - which is precisely how the unwired hardware adapter (#68) hid,
        # and removing the adoption call from registration did NOT fail this suite until
        # this check existed. Registration must be what triggers it.
        from gateway.registry import DeviceIdentity

        class FakeSlot:
            device_id = "bench-2"
            identity = DeviceIdentity(device_id="bench-2", name="Bench")

        wired = make_loop(tmp)
        wired.device_id = ""
        wired.volume = 0
        wired.registry = None
        await wired._on_device_registered(FakeSlot())
        check("registration itself makes the loop adopt the device id",
              wired.device_id == "bench-2", wired.device_id)
        check("and it picks up that device's stored level rather than the previous one",
              wired.volume == USER_MIN, wired.volume)

        # A device that reconnects keeps the same id, so adoption stays a no-op.
        await wired._on_device_registered(FakeSlot())
        check("a reconnecting device does not disturb the adopted id",
              wired.device_id == "bench-2", wired.device_id)

    print(f"volume integration checks: {checks - len(problems)}/{checks} passed")
    for problem in problems:
        print(f"  FAIL: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
