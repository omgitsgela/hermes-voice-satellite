#!/usr/bin/env python3
"""Offline checks for the gateway-facing configuration-plane controls (issue #6/#11)."""
from __future__ import annotations

import json
import pathlib
import sys

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_DIR.parent))
sys.path.insert(0, str(PLUGIN_DIR))

import voice_satellite as plugin  # noqa: E402
import voice_tool  # noqa: E402


def main() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail="") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    fleet = {"kitchen-1": {"device_id": "kitchen-1", "generation": 2, "desired": {"mic_gain": 78},
                           "reported": {"mic_gain": 78}, "health": "healthy",
                           "converged": True, "drift": {}, "connected": True}}
    calls: list[tuple[str, str, dict | None]] = []

    def request(method: str, path: str, payload=None) -> dict:
        calls.append((method, path, payload))
        if path == "/settings":
            return {"ok": True, "follow_up": False, "endpointing_profile": "standard"}
        if path == "/config/schema":
            return {"ok": True, "schema_version": 1,
                    "device_settings": {"mic_gain": {"type": "integer", "minimum": 0, "maximum": 100,
                                                     "default": 78, "apply": "immediate"}}}
        if path == "/config/devices":
            return {"ok": True, "count": len(fleet), "devices": list(fleet.values())}
        if path.startswith("/config/devices/"):
            record = fleet.get("kitchen-1")
            if method == "PUT":
                record = dict(record)
                record["desired"] = {**record["desired"], **payload["settings"]}
                record["generation"] += 1
                record["converged"] = False
                fleet["kitchen-1"] = record
                return {"ok": True, "accepted": True, "dispatched": True, "device": record}
            return {"ok": True, "device": record}
        raise AssertionError(f"unexpected path {path}")

    voice_tool._request = request

    actions = plugin._SCHEMA["parameters"]["properties"]["action"]["enum"]
    for expected in ("fleet_status", "config_status", "config_set"):
        check(f"{expected} is published in the action schema", expected in actions, actions)

    result = json.loads(voice_tool.handle({"action": "fleet_status"}))
    check("fleet_status reports devices and convergence",
          result["ok"] and result["count"] == 1
          and result["devices"][0]["converged"] is True, result)

    result = json.loads(voice_tool.handle({"action": "config_status", "device_id": "kitchen-1"}))
    check("config_status reads one device",
          result["ok"] and result["device"]["generation"] == 2, result)

    result = json.loads(voice_tool.handle(
        {"action": "config_set", "device_id": "kitchen-1", "settings": {"mic_gain": 82}}))
    check("config_set uses the current generation and reads the result back",
          result["ok"] and result["generation"] == 3
          and result["desired"]["mic_gain"] == 82
          and result["converged"] is False, result)
    check("config_set reports whether the write reached a live device",
          result["dispatched"] is True, result)
    put = [c for c in calls if c[0] == "PUT"]
    check("config_set sends the generation it read, not a guess",
          put and put[-1][2]["expected_generation"] == 2, put)

    result = json.loads(voice_tool.handle({"action": "config_set", "settings": {}}))
    check("config_set without a device or settings is refused",
          result["ok"] is False and "error" in result, result)

    result = json.loads(voice_tool.handle({"action": "config_set", "device_id": "kitchen-1",
                                           "settings": {"shell_command": "reboot"}}))
    check("config_set refuses a non-schema setting before sending",
          result["ok"] is False and "shell_command" in result["error"], result)

    result = json.loads(voice_tool.handle({"action": "nonsense"}))
    check("an unknown action is still refused", result["ok"] is False, result)

    total = 9 + len(failures)
    print(f"voice config controls: {total - len(failures)}/{total} passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
