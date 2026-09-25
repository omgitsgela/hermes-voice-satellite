#!/usr/bin/env python3
"""Bridge-side volume integration checks (issue #10).

Covers the part that ties the parser to the device: a spoken request must change the
device's level, the bridge must never claim a change it did not make, and there must be
no path from any spoken request to silence.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge.config_plane import ConfigPlane  # noqa: E402
from bridge.loop import VoiceLoop  # noqa: E402
from bridge.volume import FLOOR_PERCENT, MAX_PERCENT  # noqa: E402


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
              loop.volume >= FLOOR_PERCENT, loop.volume)
        check("'set volume to 0' tells the device a level above zero",
              loop.dispatched[-1][1] >= FLOOR_PERCENT, loop.dispatched)
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
        # The level the bridge tracks is the RECORDED intent, and the sentence says plainly
        # that it has not been applied. So the two together are honest: the durable state and
        # the tracked value agree with each other, and neither claims the device moved.
        recorded = loop.config_plane.get_device("kitchen-1")["desired"]["playback_volume"]
        check("the recorded level and the tracked level agree after a failed dispatch",
              loop.volume == recorded, (loop.volume, recorded))
        check("a failed dispatch is still recorded durably, so it converges later",
              recorded >= FLOOR_PERCENT, recorded)

        # ---------------------------------------------- no spoken request can silence it
        requests = ["set volume to 0", "set volume to 0%", "volume down", "volume down by 9",
                    "quieter", "set volume to 1%", "volume down by two"]
        for text in requests:
            loop = make_loop(tmp)
            # Start as quiet as the floor allows, the worst case.
            loop.volume = FLOOR_PERCENT
            await loop._volume_command(text)
            check(f"{text!r} from the floor stays audible", loop.volume >= FLOOR_PERCENT, loop.volume)
            if loop.dispatched:
                check(f"{text!r} never sends a silent level to the device",
                      loop.dispatched[-1][1] >= FLOOR_PERCENT, loop.dispatched)

        # Start loud, step all the way down, and confirm it stops at the floor.
        loop = make_loop(tmp)
        loop.volume = MAX_PERCENT
        for _ in range(30):
            await loop._volume_command("volume down")
        check("stepping down repeatedly stops at the floor and never below",
              loop.volume == FLOOR_PERCENT, loop.volume)
        check("every level sent to the device during the sweep was audible",
              all(level >= FLOOR_PERCENT for _id, level in loop.dispatched), loop.dispatched)

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
              loaded >= FLOOR_PERCENT, loaded)
        check("the loop's tracked level is the raised one, not the stored zero",
              loader.volume == loaded, (loader.volume, loaded))

        # And a stored value below a measured floor is raised to it too.
        plane.set_desired("kitchen-1", {"playback_volume": 1})
        loader2 = VoiceLoop.__new__(VoiceLoop)
        loader2.config_plane = ConfigPlane(hostile)
        loader2.device_id = "kitchen-1"
        check("a stored level below the floor is raised on load",
              loader2._load_volume(loader2) >= FLOOR_PERCENT, loader2.volume)

    print(f"volume integration checks: {checks - len(problems)}/{checks} passed")
    for problem in problems:
        print(f"  FAIL: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
