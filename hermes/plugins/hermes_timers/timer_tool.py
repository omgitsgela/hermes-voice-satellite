"""Implementation for the hermes_timers plugin."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# The checkout this installation lives in. A deployment value, not a literal: an environment
# variable wins, and the default is the conventional location (issue #45).
_PROJECT = Path(os.environ.get("VOICE_BRIDGE_PROJECT", "").strip() or (Path.home() / "hermes-voice-bridge"))
TOKEN_FILE = _PROJECT / "state" / "announce-token.txt"
TOKEN_ENV = "VOICE_ANNOUNCE_TOKEN"
BASE_URL = os.environ.get("VOICE_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
TIMEOUT = 10.0
try:
    from hermes_constants import get_hermes_home
    CRON_JOBS_FILE = get_hermes_home() / "cron" / "jobs.json"
except Exception:
    CRON_JOBS_FILE = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "cron" / "jobs.json"


class TimerToolError(RuntimeError):
    pass


def _token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError as exc:
        raise TimerToolError("the bridge token file is not readable") from exc
    if not token:
        raise TimerToolError("the bridge token file is empty")
    return token


def _request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_token()}"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("error")
        except Exception:
            detail = f"HTTP {exc.code}"
        raise TimerToolError(str(detail or f"HTTP {exc.code}")) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise TimerToolError("the bridge timer endpoint is unavailable") from exc


def _alarm_list() -> list[dict]:
    try:
        payload = json.loads(CRON_JOBS_FILE.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError) as exc:
        raise TimerToolError("the Hermes cron job inventory is unreadable") from exc
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    alarms = []
    for job in jobs:
        if not isinstance(job, dict) or not str(job.get("name") or "").lower().startswith("alarm:"):
            continue
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        alarms.append({
            "id": job.get("id"),
            "name": job.get("name"),
            "enabled": bool(job.get("enabled")),
            "state": job.get("state"),
            "schedule": job.get("schedule_display") or schedule.get("display"),
            "next_run_at": job.get("next_run_at"),
            "last_run_at": job.get("last_run_at"),
            "last_status": job.get("last_status"),
        })
    return alarms


def handle(args: dict) -> str:
    args = args or {}
    action = str(args.get("action") or "")
    try:
        if action == "timer_list":
            result = _request("GET", "/timers")
        elif action == "timer_set":
            seconds = int(args.get("seconds") or 0)
            if seconds <= 0:
                raise TimerToolError("seconds must be greater than zero")
            result = _request("POST", "/timers", {
                "action": "set", "seconds": seconds,
                "label": " ".join(str(args.get("label") or "").split())[:120],
            })
        elif action == "timer_cancel":
            body = {"action": "cancel"}
            selectors = 0
            if args.get("id"):
                body["id"] = str(args["id"])
                selectors += 1
            if args.get("label"):
                body["label"] = str(args["label"])
                selectors += 1
            if args.get("all") is True:
                body["all"] = True
                selectors += 1
            if selectors != 1:
                raise TimerToolError("provide exactly one of id, label, or all")
            result = _request("POST", "/timers", body)
        elif action == "alarm_list":
            alarms = _alarm_list()
            result = {"ok": True, "count": len(alarms), "alarms": alarms,
                      "category": "cron jobs whose names begin with alarm:"}
        else:
            raise TimerToolError(
                "unknown action; expected timer_set, timer_list, timer_cancel, or alarm_list")
        return json.dumps(result, ensure_ascii=False)
    except TimerToolError as exc:
        return json.dumps({"ok": False, "action": action, "error": str(exc)}, ensure_ascii=False)


__all__ = ["handle", "TimerToolError", "BASE_URL", "TOKEN_FILE", "CRON_JOBS_FILE"]
