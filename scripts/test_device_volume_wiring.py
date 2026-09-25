#!/usr/bin/env python3
"""The outbound runner must actually apply settings, not just stage them (#10).

This exists because the adapter was written, tested, and then never wired in: the runner
constructed its configuration agent without an `apply` callback, so a volume change would
have been staged forever and reported `degraded` while the adapter's own tests passed.

A tested component that nothing calls is not a feature. This pins the wiring.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wyoming.event import Event  # noqa: E402

from pi.outbound_satellite import BridgeLink, LinkConfig  # noqa: E402
from pi.volume_control import AlsaVolume  # noqa: E402

problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


class FakeAmixer:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()


def config_for(tmp: str) -> LinkConfig:
    return LinkConfig(
        device_id="kitchen-1",
        host="127.0.0.1",
        port=1,
        relay=False,
        config_state_path=pathlib.Path(tmp) / "config.json",
    )


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        # ---------------------------------------------- default construction applies
        fake = FakeAmixer()
        link = BridgeLink(
            config_for(tmp),
            alsa_volume=AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake),
        )
        report = await link.config_agent.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1,
            "device_id": "kitchen-1",
            "generation": 1,
            "settings": {"playback_volume": 70},
        }))
        check("a volume change through the runner is applied, not staged",
              report.data["actual"].get("playback_volume") == 70, report.data)
        check("the runner reported it healthy rather than degraded",
              report.data["health"] == "healthy", report.data)
        check("the device was actually told",
              any("sset" in call for call in fake.calls), fake.calls)
        check("the level was persisted for reboot survival",
              any("alsactl" in call[0] for call in fake.calls), fake.calls)

        # ---------------------------------------------- the floor still holds here
        fake2 = FakeAmixer()
        link2 = BridgeLink(
            LinkConfig(
                device_id="kitchen-1", host="127.0.0.1", port=1, relay=False,
                config_state_path=pathlib.Path(tmp) / "config2.json"),
            alsa_volume=AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake2),
        )
        report = await link2.config_agent.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1,
            "device_id": "kitchen-1",
            "generation": 1,
            "settings": {"playback_volume": 0},
        }))
        check("a zero through the runner lands on the floor",
              report.data["actual"].get("playback_volume") == 25, report.data)
        check("a zero through the runner never reaches amixer as 0%",
              not any("0%" in part for call in fake2.calls for part in call), fake2.calls)

        # ---------------------------------------------- an explicit agent still wins
        class StubAgent:
            async def apply_event(self, _event):
                return Event(type="hermes.config.reported", data={
                    "generation": 9, "actual": {}, "health": "healthy", "error": None})

        stub = StubAgent()
        link3 = BridgeLink(config_for(str(tmp)), config_agent=stub)
        check("an explicitly supplied agent is used as-is, so tests and callers can override",
              link3.config_agent is stub, type(link3.config_agent))

    print(f"device volume wiring checks: {checks - len(problems)}/{checks} passed")
    for problem in problems:
        print(f"  FAIL: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
