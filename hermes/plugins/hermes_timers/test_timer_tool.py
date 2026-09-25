#!/usr/bin/env python3
"""Offline checks for the Hermes timer/alarm tool (issue #5)."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_DIR.parent))
sys.path.insert(0, str(PLUGIN_DIR))

import hermes_timers as plugin  # noqa: E402
import timer_tool  # noqa: E402


def main() -> int:
    failures: list[str] = []
    calls: list[tuple[str, str, dict | None]] = []

    def request(method: str, path: str, payload=None) -> dict:
        calls.append((method, path, payload))
        if method == "GET":
            return {"ok": True, "count": 1, "timers": [{"id": "t1", "remaining_seconds": 30}]}
        if payload.get("action") == "set":
            return {"ok": True, "timer": {"id": "t2", "duration_seconds": payload.get("seconds", 90)}}
        if payload.get("action") == "cancel":
            return {"ok": True, "cancelled": 1, "timers": [{"id": "t1"}]}
        raise AssertionError(payload)

    timer_tool._request = request
    actions = set(plugin._SCHEMA["parameters"]["properties"]["action"]["enum"])
    if actions != {"timer_set", "timer_list", "timer_cancel", "alarm_list"}:
        failures.append(f"schema actions: {actions!r}")

    result = json.loads(timer_tool.handle({"action": "timer_set", "seconds": 90, "label": "tea"}))
    if not (result.get("ok") and calls[-1] ==
            ("POST", "/timers", {"action": "set", "seconds": 90, "label": "tea"})):
        failures.append(f"timer_set: {result!r}, call={calls[-1]!r}")

    result = json.loads(timer_tool.handle({"action": "timer_list"}))
    if not (result.get("ok") and result.get("count") == 1 and calls[-1][0:2] == ("GET", "/timers")):
        failures.append(f"timer_list: {result!r}")

    result = json.loads(timer_tool.handle({"action": "timer_cancel", "label": "tea"}))
    if not (result.get("ok") and result.get("cancelled") == 1):
        failures.append(f"timer_cancel: {result!r}")

    with tempfile.TemporaryDirectory() as tmp:
        jobs = pathlib.Path(tmp) / "jobs.json"
        jobs.write_text(json.dumps({"jobs": [
            {"id": "a1", "name": "alarm: weekday wake", "enabled": True,
             "schedule_display": "weekdays at 6:30am", "next_run_at": "tomorrow",
             "last_status": "ok"},
            {"id": "n1", "name": "inbox sweep", "enabled": True,
             "schedule_display": "hourly"},
            {"id": "a2", "name": "Alarm: tea", "enabled": False,
             "schedule": {"display": "once"}},
        ]}))
        timer_tool.CRON_JOBS_FILE = jobs
        result = json.loads(timer_tool.handle({"action": "alarm_list"}))
        names = [job["name"] for job in result.get("alarms", [])]
        if names != ["alarm: weekday wake", "Alarm: tea"]:
            failures.append(f"alarm_list leaked or missed jobs: {result!r}")

    invalid = json.loads(timer_tool.handle({"action": "timer_set", "seconds": 0}))
    if invalid.get("ok") is not False:
        failures.append(f"invalid set: {invalid!r}")

    print(f"timer tool checks: {6 - len(failures)}/6 passed")
    for failure in failures:
        print("  FAIL:", failure)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
