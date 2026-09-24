#!/usr/bin/env python3
"""Offline checks for voice-satellite endpointing-profile controls (issue #26)."""
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
    state = {"follow_up": False, "endpointing_profile": "standard"}

    def request(method: str, path: str, payload=None) -> dict:
        assert path == "/settings"
        if method == "POST":
            assert isinstance(payload, dict)
            state.update(payload)
        return {"ok": True, **state}

    voice_tool._request = request
    failures: list[str] = []

    actions = plugin._SCHEMA["parameters"]["properties"]["action"]["enum"]
    expected_actions = {
        "follow_up_on", "follow_up_off", "follow_up_status",
        "endpointing_pause_tolerant", "endpointing_standard", "endpointing_status",
    }
    if set(actions) != expected_actions:
        failures.append(f"published action schema: {actions!r}")

    result = json.loads(voice_tool.handle({"action": "endpointing_status"}))
    if result != {"ok": True, "action": "endpointing_status",
                  "endpointing_profile": "standard", "verified": True}:
        failures.append(f"status: {result!r}")

    result = json.loads(voice_tool.handle({"action": "endpointing_pause_tolerant"}))
    if not (result.get("ok") and result.get("verified")
            and result.get("endpointing_profile") == "pause_tolerant"):
        failures.append(f"pause-tolerant set: {result!r}")

    result = json.loads(voice_tool.handle({"action": "endpointing_standard"}))
    if not (result.get("ok") and result.get("verified")
            and result.get("endpointing_profile") == "standard"):
        failures.append(f"standard set: {result!r}")

    # Existing controls must still use the same read-back path.
    result = json.loads(voice_tool.handle({"action": "follow_up_on"}))
    if not (result.get("ok") and result.get("verified") and result.get("follow_up") is True):
        failures.append(f"follow-up compatibility: {result!r}")

    print(f"voice profile controls: {5 - len(failures)}/5 passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
