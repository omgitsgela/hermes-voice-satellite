#!/usr/bin/env python3
"""Device-side desired-state validation and reporting checks (#6/#11)."""
from __future__ import annotations

import asyncio
import pathlib
import stat
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wyoming.event import Event  # noqa: E402

from pi.config_agent import DeviceConfigAgent  # noqa: E402


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
        path = pathlib.Path(tmp) / "device-config.json"
        applied: list[dict] = []

        async def apply(settings: dict):
            applied.append(dict(settings))
            return dict(settings), "healthy", None

        agent = DeviceConfigAgent(path, apply=apply)
        event = Event(type="hermes.config.desired", data={
            "schema_version": 1,
            "device_id": "kitchen-1",
            "generation": 2,
            "settings": {"wake_word": "hey_hermes", "mic_gain": 78},
        })
        report = await agent.apply_event(event)
        check("valid desired state is applied once", len(applied) == 1, applied)
        check("reported event confirms actual state",
              report.type == "hermes.config.reported"
              and report.data["generation"] == 2
              and report.data["actual"] == event.data["settings"]
              and report.data["health"] == "healthy", report)
        check("device state is private and durable",
              path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600,
              oct(stat.S_IMODE(path.stat().st_mode)) if path.exists() else "missing")

        same = await agent.apply_event(event)
        check("same generation is idempotent", len(applied) == 1 and same.data == report.data,
              {"applied": applied, "report": same.data})

        invalid = await agent.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1,
            "device_id": "kitchen-1",
            "generation": 3,
            "settings": {"shell_command": "reboot"},
        }))
        check("invalid desired state is rejected without applying",
              len(applied) == 1 and invalid.data["health"] == "error"
              and invalid.data["generation"] == 2, invalid)

        wrong_device = await agent.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1,
            "device_id": "study-1",
            "generation": 3,
            "settings": {"mic_gain": 80},
        }))
        check("desired state for another device is rejected",
              wrong_device.data["health"] == "error" and len(applied) == 1, wrong_device)

        restarted = DeviceConfigAgent(path, apply=apply)
        check("reported state survives process restart",
              restarted.current_report().data["generation"] == 2)

        staged_path = pathlib.Path(tmp) / "staged-only.json"
        staged = DeviceConfigAgent(staged_path, device_id="kitchen-2")
        staged_report = await staged.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1,
            "device_id": "kitchen-2",
            "generation": 1,
            "settings": {"mic_gain": 70},
        }))
        check("a device with no hardware adapter reports staged state honestly",
              staged_report.data["health"] == "degraded"
              and staged_report.data["actual"] == {}
              and "adapter" in staged_report.data["error"], staged_report)

    if problems:
        print("\nFAILURES")
        for problem in problems:
            print(" -", problem)
        return 1
    print(f"\ndevice config checks: {checks}/{checks} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
