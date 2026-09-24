"""Control for the voice satellite itself (follow-up mode), for the assistant to call.

Why this exists, in Angela's words (2026-09-22): the bridge can act on the literal phrase "turn on
follow up mode" by itself, but a phrase match is not understanding. Giving the assistant a tool
means any natural way of asking works: "keep listening after you answer", "stop following up", "are
you still listening for replies?". The bridge keeps its own matcher as an instant, offline fallback
for the exact phrases; this tool is the general path.

Wire protocol, on the bridge's existing LAN port (the server that already serves announcements):

    GET  /settings                     -> {"ok": true, "follow_up": false}
    POST /settings {"follow_up": true} -> {"ok": true, "follow_up": true}

Bearer token: the same one announcements use, read from the bridge's state directory. It is never
logged, never returned to a caller and never handed to a model.

Every change is read back from the bridge before it is reported, and the reply carries the resulting
state, so the assistant never has to claim a success it did not see.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

_PROJECT = Path("/home/angela/hermes-voice-bridge")
TOKEN_FILE = _PROJECT / "state" / "announce-token.txt"
TOKEN_ENV = "VOICE_ANNOUNCE_TOKEN"
BASE_URL = os.environ.get("VOICE_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
TIMEOUT = 10.0

_ACTIONS = (
    "follow_up_on",
    "follow_up_off",
    "follow_up_status",
    "endpointing_pause_tolerant",
    "endpointing_standard",
    "endpointing_status",
)


class VoiceControlError(RuntimeError):
    """A refusal or a failed change, with a message safe to show a caller."""


def _token() -> str:
    from_env = os.environ.get(TOKEN_ENV, "").strip()
    if from_env:
        return from_env
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError as exc:
        raise VoiceControlError("the bridge's token file is not readable") from exc
    if not token:
        raise VoiceControlError("the bridge's token file is empty")
    return token


def _request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    """One call against the bridge. The token rides in a header, never in a URL or a log line."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_token()}"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise VoiceControlError("the bridge refused the token") from exc
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:160]
        except Exception:  # noqa: BLE001
            pass
        raise VoiceControlError(f"the bridge answered {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise VoiceControlError("the bridge is not answering on its settings port") from exc


def _settings_data() -> dict:
    """The device's current settings, straight from the bridge."""
    data = _request("GET", "/settings")
    if not data.get("ok"):
        raise VoiceControlError(str(data.get("error") or "the bridge refused the read"))
    return data


def _settings() -> bool:
    """The device's current follow-up state, straight from the bridge."""
    return bool(_settings_data().get("follow_up"))


def _endpointing_profile() -> str:
    profile = str(_settings_data().get("endpointing_profile") or "")
    if profile not in ("standard", "pause_tolerant"):
        raise VoiceControlError("the bridge reported an unknown endpointing profile")
    return profile


def _set_follow_up(on: bool) -> Any:
    """Change the mode, then read it back so the answer is the bridge's state, not our hope."""
    data = _request("POST", "/settings", {"follow_up": on})
    if not data.get("ok"):
        raise VoiceControlError(str(data.get("error") or "the bridge refused the change"))
    verified = _settings()
    if verified != on:
        raise VoiceControlError(
            f"asked for follow-up {'on' if on else 'off'} but the device reports "
            f"{'on' if verified else 'off'}")
    return verified


def _set_endpointing_profile(profile: str) -> str:
    """Change the profile, then read it back from the bridge before reporting success."""
    data = _request("POST", "/settings", {"endpointing_profile": profile})
    if not data.get("ok"):
        raise VoiceControlError(str(data.get("error") or "the bridge refused the change"))
    verified = _endpointing_profile()
    if verified != profile:
        raise VoiceControlError(
            f"asked for endpointing profile {profile!r} but the device reports {verified!r}")
    return verified


def handle(args: dict) -> str:
    action = str((args or {}).get("action") or "").strip()
    if action not in _ACTIONS:
        return json.dumps({"ok": False,
                           "error": f"unknown action {action!r}; expected one of {list(_ACTIONS)}"},
                          ensure_ascii=False)
    try:
        if action == "follow_up_status":
            state = _settings()
            return json.dumps({"ok": True, "action": action, "follow_up": state,
                               "verified": True}, ensure_ascii=False)
        if action == "endpointing_status":
            profile = _endpointing_profile()
            return json.dumps({"ok": True, "action": action,
                               "endpointing_profile": profile, "verified": True},
                              ensure_ascii=False)
        if action.startswith("endpointing_"):
            wanted = ("pause_tolerant" if action == "endpointing_pause_tolerant"
                      else "standard")
            profile = _set_endpointing_profile(wanted)
            return json.dumps({"ok": True, "action": action,
                               "endpointing_profile": profile, "verified": True},
                              ensure_ascii=False)
        wanted = action == "follow_up_on"
        state = _set_follow_up(wanted)
        return json.dumps({"ok": True, "action": action, "follow_up": state, "verified": True},
                          ensure_ascii=False)
    except VoiceControlError as exc:
        return json.dumps({"ok": False, "action": action, "error": str(exc)}, ensure_ascii=False)


__all__ = ["handle", "VoiceControlError", "BASE_URL", "TOKEN_FILE"]
