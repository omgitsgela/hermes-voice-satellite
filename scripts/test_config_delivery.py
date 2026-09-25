#!/usr/bin/env python3
"""Host-side desired/reported state delivery checks (#6/#11)."""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wyoming.event import Event  # noqa: E402

from bridge.config_plane import ConfigPlane  # noqa: E402
from bridge.loop import VoiceLoop  # noqa: E402
from bridge.registry import CONFIG_DESIRED_EVENT, CONFIG_REPORTED_EVENT, DeviceIdentity  # noqa: E402


class FakeConnection:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def send(self, event: Event) -> bool:
        self.events.append(event)
        return True


class FakeSlot:
    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.connection = FakeConnection()
        self.identity = DeviceIdentity(
            device_id=device_id, name="Kitchen", area="kitchen",
            firmware_version="1.2.3", capabilities=("speaker", "led"), boot_id="boot-1",
        )


class FakeRegistry:
    def __init__(self, slot: FakeSlot) -> None:
        self.slot = slot

    def get(self, device_id: str):
        return self.slot if device_id == self.slot.device_id else None

    def connected_devices(self):
        return [self.slot.device_id]


async def main() -> int:
    problems: list[str] = []
    checks = 0

    def check(label: str, condition: bool, detail="") -> None:
        nonlocal checks
        checks += 1
        print(f"{'PASS' if condition else 'FAIL'} {label}")
        if not condition:
            problems.append(f"{label}: {detail}")

    with tempfile.TemporaryDirectory() as tmp:
        loop = VoiceLoop.__new__(VoiceLoop)
        loop.config_plane = ConfigPlane(pathlib.Path(tmp) / "fleet.json")
        # The loop tracks which device it is addressing; registration sets it (see
        # _adopt_device). A bare loop built with __new__ has to provide it, or registration
        # fails on a missing attribute before it can do its work.
        loop.device_id = ""
        slot = FakeSlot("kitchen-1")
        loop.registry = FakeRegistry(slot)

        await loop._on_device_registered(slot)
        check("registration seeds and dispatches desired state", len(slot.connection.events) == 1,
              slot.connection.events)
        desired_event = slot.connection.events[0]
        check("desired event carries the complete generation",
              desired_event.type == CONFIG_DESIRED_EVENT
              and desired_event.data["generation"] == 1
              and desired_event.data["settings"]["wake_word"] == "hey_hermes",
              desired_event)

        changed = loop.config_plane.set_desired(
            "kitchen-1", {"mic_gain": 82}, expected_generation=1)
        sent = await loop._dispatch_device_config("kitchen-1", changed)
        check("API writes dispatch over the live device connection",
              sent is True and slot.connection.events[-1].data["generation"] == 2,
              slot.connection.events[-1])

        report = Event(type=CONFIG_REPORTED_EVENT, data={
            "generation": 2,
            "actual": changed["desired"],
            "health": "healthy",
            "error": None,
        })
        handled = await loop._on_device_control_event("kitchen-1", report)
        current = loop.config_plane.get_device("kitchen-1")
        check("device reports update actual state", handled is True and current["converged"] is True,
              current)

        ignored = await loop._on_device_control_event("kitchen-1", Event(type="audio-chunk"))
        check("non-configuration events are not consumed", ignored is False)

    if problems:
        print("\nFAILURES")
        for problem in problems:
            print(" -", problem)
        return 1
    print(f"\nconfiguration delivery checks: {checks}/{checks} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
