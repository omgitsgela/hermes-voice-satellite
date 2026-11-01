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
    checks_seen: list[str] = []

    def check(label: str, condition: bool, detail="") -> None:
        checks_seen.append(label)
        if not condition:
            failures.append(f"{label}: {detail}")

    fleet = {"kitchen-1": {"device_id": "kitchen-1", "generation": 2, "desired": {"mic_volume": 78},
                           "reported": {"mic_volume": 78}, "health": "healthy",
                           "converged": True, "drift": {}, "connected": True},
             "hall-1": {"device_id": "hall-1", "generation": 2, "desired": {"mic_volume": 78},
                        "reported": {"mic_volume": 78}, "health": "healthy",
                        "converged": True, "drift": {}, "connected": True}}
    calls: list[tuple[str, str, dict | None]] = []

    def request(method: str, path: str, payload=None) -> dict:
        calls.append((method, path, payload))
        if path == "/settings":
            return {"ok": True, "follow_up": False, "endpointing_profile": "standard"}
        if path == "/config/schema":
            return {"ok": True, "schema_version": 1,
                    "device_settings": {"mic_volume": {"type": "integer", "minimum": 0, "maximum": 100,
                                                     "default": 78, "apply": "immediate"}}}
        if path == "/config/devices":
            return {"ok": True, "count": len(fleet), "devices": list(fleet.values())}
        if path == "/config/fleet/desired":
            targets = payload.get("device_ids") or list(fleet)
            if any(t not in fleet for t in targets):
                unknown = sorted(t for t in targets if t not in fleet)
                return {"ok": False, "error": f"unknown target {unknown[0]!r}"}
            restart = any(k in ("wake_word", "wake_threshold", "led_brightness")
                          for k in payload.get("settings", {}))
            records = []
            for device_id in targets:
                record = dict(fleet[device_id])
                record["desired"] = {**record["desired"], **payload["settings"]}
                record["generation"] += 1
                record["converged"] = False
                fleet[device_id] = record
                records.append({**record, "dispatched": True})
            return {"ok": True, "accepted": True, "count": len(records),
                    "restart_pacing": restart, "devices": records}
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
    for expected in ("fleet_status", "config_status", "config_set", "fleet_config_set"):
        check(f"{expected} is published in the action schema", expected in actions, actions)

    result = json.loads(voice_tool.handle({"action": "fleet_status"}))
    check("fleet_status reports devices and convergence",
          result["ok"] and result["count"] == 2
          and all(d["converged"] is True for d in result["devices"]), result)

    result = json.loads(voice_tool.handle({
        "action": "fleet_config_set", "settings": {"mic_volume": 82}}))
    check("fleet_config_set advances every registered device by one generation",
          result["ok"] and result["count"] == 2
          and all(d["generation"] == 3 for d in result["devices"]), result)
    check("fleet_config_set reads the fleet back rather than trusting the write",
          result["verified"] is True, result)
    check("an immediate fleet write is not paced",
          result["restart_pacing"] is False, result)
    put = [c for c in calls if c[1] == "/config/fleet/desired"]
    check("fleet_config_set targets the whole fleet when device_ids is absent",
          put and put[-1][2].get("device_ids") is None, put)

    result = json.loads(voice_tool.handle({
        "action": "fleet_config_set", "settings": {"led_brightness": 20},
        "device_ids": ["kitchen-1"]}))
    check("a fleet subset write names only its target",
          result["ok"] and result["count"] == 1
          and result["devices"][0]["device_id"] == "kitchen-1", result)
    check("a restart-class fleet write is flagged as paced",
          result["restart_pacing"] is True, result)
    put = [c for c in calls if c[1] == "/config/fleet/desired"]
    check("a fleet subset write sends the subset",
          put and put[-1][2]["device_ids"] == ["kitchen-1"], put)

    result = json.loads(voice_tool.handle({
        "action": "fleet_config_set", "settings": {"mic_volume": 5},
        "device_ids": ["ghost-1"]}))
    check("a fleet write to an unknown target is refused and not reported verified",
          result["ok"] is False and "ghost-1" in result["error"], result)

    result = json.loads(voice_tool.handle({"action": "fleet_config_set", "settings": {}}))
    check("a fleet write with no settings is refused", result["ok"] is False, result)

    result = json.loads(voice_tool.handle({
        "action": "fleet_config_set", "settings": {"shell_command": "reboot"}}))
    check("fleet_config_set refuses a non-schema setting before sending",
          result["ok"] is False and "shell_command" in result["error"], result)

    result = json.loads(voice_tool.handle({"action": "config_status", "device_id": "kitchen-1"}))
    check("config_status reads one device",
          result["ok"] and result["device"]["generation"] == 4, result)

    result = json.loads(voice_tool.handle(
        {"action": "config_set", "device_id": "kitchen-1", "settings": {"mic_volume": 82}}))
    check("config_set uses the current generation and reads the result back",
          result["ok"] and result["generation"] == 5
          and result["desired"]["mic_volume"] == 82
          and result["converged"] is False, result)
    check("config_set reports whether the write reached a live device",
          result["dispatched"] is True, result)
    put = [c for c in calls if c[0] == "PUT" and c[1].startswith("/config/devices/")]
    check("config_set sends the generation it read, not a guess",
          put and put[-1][2]["expected_generation"] == 4, put)

    result = json.loads(voice_tool.handle({"action": "config_set", "settings": {}}))
    check("config_set without a device or settings is refused",
          result["ok"] is False and "error" in result, result)

    result = json.loads(voice_tool.handle({"action": "config_set", "device_id": "kitchen-1",
                                           "settings": {"shell_command": "reboot"}}))
    check("config_set refuses a non-schema setting before sending",
          result["ok"] is False and "shell_command" in result["error"], result)

    result = json.loads(voice_tool.handle({"action": "nonsense"}))
    check("an unknown action is still refused", result["ok"] is False, result)

    checks_run = len(checks_seen)
    print(f"voice config controls: {checks_run - len(failures)}/{checks_run} passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
