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
            "settings": {"wake_word": "hey_hermes", "mic_volume": 78},
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
            "settings": {"mic_volume": 80},
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
            "settings": {"mic_volume": 70},
        }))
        check("a device with no hardware adapter reports staged state honestly",
              staged_report.data["health"] == "degraded"
              and staged_report.data["actual"] == {}
              and "adapter" in staged_report.data["error"], staged_report)

        # ------------------------------------------------------------------
        # A failed restart-class application is remembered across process
        # recreation (review F2): the unapplied set is persisted, and a fresh
        # agent hands it to a compatible adapter as the stale seed, so the new
        # adapter re-attempts the restart instead of trusting the env file.
        import json as _json
        restart_path = pathlib.Path(tmp) / "restart.json"
        adapter_calls: list[dict] = []

        def adapter_with_seed(settings, stale_seed=None):
            adapter_calls.append({"settings": settings, "stale_seed": stale_seed})
            return {}, "error", "restart failed"

        agent_a = DeviceConfigAgent(restart_path, apply=adapter_with_seed,
                                    device_id="kitchen-1")
        report_a = await agent_a.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1, "device_id": "kitchen-1", "generation": 1,
            "settings": {"led_brightness": 20},
        }))
        check("a failed application is not reported as applied",
              report_a.data["health"] == "error"
              and report_a.data["actual"].get("led_brightness") is None, report_a)
        persisted = _json.loads(restart_path.read_text())
        check("the unapplied set is persisted with the device state",
              persisted.get("pending_restart") == ["led_brightness"], persisted)

        # A fresh agent (process restarted) re-delivering the same generation must
        # hand the persisted stale set to the adapter.
        agent_b = DeviceConfigAgent(restart_path, apply=adapter_with_seed,
                                    device_id="kitchen-1")
        report_b = await agent_b.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1, "device_id": "kitchen-1", "generation": 1,
            "settings": {"led_brightness": 20},
        }))
        check("a fresh agent re-attempts a failed restart-class setting",
              report_b.data["health"] == "error" and len(adapter_calls) == 2,
              (report_b, adapter_calls))
        check("the fresh agent's adapter received the persisted stale seed",
              adapter_calls[1].get("stale_seed") == ["led_brightness"], adapter_calls)

        # A successful application clears the remembered set.
        def adapter_ok(settings, stale_seed=None):
            return dict(settings), "healthy", None

        agent_c = DeviceConfigAgent(restart_path, apply=adapter_ok,
                                    device_id="kitchen-1")
        report_c = await agent_c.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1, "device_id": "kitchen-1", "generation": 2,
            "settings": {"led_brightness": 20},
        }))
        persisted_c = _json.loads(restart_path.read_text())
        check("a successful application clears the unapplied set",
              report_c.data["health"] == "healthy"
              and persisted_c.get("pending_restart") == [], persisted_c)

        # An adapter without the seed parameter is called the old way (staging
        # fallback compatibility).
        no_seed_path = pathlib.Path(tmp) / "no-seed.json"
        legacy_calls: list[dict] = []

        def adapter_legacy(settings):
            legacy_calls.append(dict(settings))
            return {}, "degraded", "no adapter"

        legacy_agent = DeviceConfigAgent(no_seed_path, apply=adapter_legacy,
                                         device_id="kitchen-3")
        legacy_agent.state["pending_restart"] = ["wake_word"]
        legacy_agent._save()
        await legacy_agent.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1, "device_id": "kitchen-3", "generation": 1,
            "settings": {"wake_word": "hey_karen"},
        }))
        check("an adapter without a stale-seed parameter is called normally",
              len(legacy_calls) == 1 and legacy_calls[0] == {"wake_word": "hey_karen"},
              legacy_calls)

    if problems:
        print("\nFAILURES")
        for problem in problems:
            print(" -", problem)
        return 1
    print(f"\ndevice config checks: {checks}/{checks} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
