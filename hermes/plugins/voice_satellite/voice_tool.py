"""Control for the voice satellite itself (follow-up mode), for the assistant to call.

Why this exists, in the request that made it (2026-09-22): the Gateway can act on the literal phrase "turn on
follow up mode" by itself, but a phrase match is not understanding. Giving the assistant a tool
means any natural way of asking works: "keep listening after you answer", "stop following up", "are
you still listening for replies?". The Gateway keeps its own matcher as an instant, offline fallback
for the exact phrases; this tool is the general path.

Wire protocol, on the Gateway's existing LAN port (the server that already serves announcements):

    GET  /settings                     -> {"ok": true, "follow_up": false}
    POST /settings {"follow_up": true} -> {"ok": true, "follow_up": true}

Bearer token: the same one announcements use, read from the Gateway's state directory. It is never
logged, never returned to a caller and never handed to a model.

Every change is read back from the Gateway before it is reported, and the reply carries the resulting
state, so the assistant never has to claim a success it did not see.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

# The checkout this installation lives in. A deployment value, not a literal: an environment
# variable wins, and the default is the conventional location (issue #45).
_PROJECT = Path(os.environ.get("VOICE_GATEWAY_PROJECT", "").strip() or (Path.home() / "hermes-voice-satellite"))
TOKEN_FILE = _PROJECT / "state" / "announce-token.txt"
TOKEN_ENV = "VOICE_ANNOUNCE_TOKEN"
BASE_URL = os.environ.get("VOICE_GATEWAY_URL", "http://127.0.0.1:8765").rstrip("/")
TIMEOUT = 10.0

_ACTIONS = (
    "follow_up_on",
    "follow_up_off",
    "follow_up_status",
    "endpointing_pause_tolerant",
    "endpointing_standard",
    "endpointing_status",
    "conversation_reset",
    "fleet_status",
    "config_status",
    "config_set",
    "fleet_config_set",
)

# Settings the gateway tool will pass through. The Gateway validates authoritatively; this list exists so a
# typo is caught here, with a useful message, instead of being sent and refused as an opaque 400.
_SETTINGS = (
    "wake_word",
    "wake_threshold",
    "mic_gain",
    "playback_volume",
    "led_brightness",
    "locale",
    "update_channel",
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
        raise VoiceControlError("the Gateway's token file is not readable") from exc
    if not token:
        raise VoiceControlError("the Gateway's token file is empty")
    return token


def _request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    """One call against the gateway. The token rides in a header, never in a URL or a log line."""
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
            raise VoiceControlError("the Gateway refused the token") from exc
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:160]
        except Exception:  # noqa: BLE001
            pass
        raise VoiceControlError(f"the Gateway answered {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise VoiceControlError("the Gateway is not answering on its settings port") from exc


def _settings_data() -> dict:
    """The device's current settings, straight from the gateway."""
    data = _request("GET", "/settings")
    if not data.get("ok"):
        raise VoiceControlError(str(data.get("error") or "the Gateway refused the read"))
    return data


def _settings() -> bool:
    """The device's current follow-up state, straight from the gateway."""
    return bool(_settings_data().get("follow_up"))


def _endpointing_profile() -> str:
    profile = str(_settings_data().get("endpointing_profile") or "")
    if profile not in ("standard", "pause_tolerant"):
        raise VoiceControlError("the Gateway reported an unknown endpointing profile")
    return profile


def _set_follow_up(on: bool) -> Any:
    """Change the mode, then read it back so the answer is the Gateway's state, not our hope."""
    data = _request("POST", "/settings", {"follow_up": on})
    if not data.get("ok"):
        raise VoiceControlError(str(data.get("error") or "the Gateway refused the change"))
    verified = _settings()
    if verified != on:
        raise VoiceControlError(
            f"asked for follow-up {'on' if on else 'off'} but the device reports "
            f"{'on' if verified else 'off'}")
    return verified


def _set_endpointing_profile(profile: str) -> str:
    """Change the profile, then read it back from the Gateway before reporting success."""
    data = _request("POST", "/settings", {"endpointing_profile": profile})
    if not data.get("ok"):
        raise VoiceControlError(str(data.get("error") or "the Gateway refused the change"))
    verified = _endpointing_profile()
    if verified != profile:
        raise VoiceControlError(
            f"asked for endpointing profile {profile!r} but the device reports {verified!r}")
    return verified


def _fleet() -> dict:
    """Every device the Gateway knows about, with its convergence state."""
    data = _request("GET", "/config/devices")
    if not data.get("ok"):
        raise VoiceControlError(str(data.get("error") or "the Gateway refused the fleet read"))
    return data


def _device(device_id: str) -> dict:
    data = _request("GET", f"/config/devices/{device_id}")
    if not data.get("ok"):
        raise VoiceControlError(str(data.get("error") or "the Gateway refused the device read"))
    return data["device"]


def _set_config(device_id: str, settings: dict) -> dict:
    """Write desired state against the generation just read, then read the result back.

    The generation guard is the point: a stale `expected_generation` is a 409, which is how two writers
    are stopped from silently overwriting each other. Reading first and sending that exact number means
    this tool never clobbers a change it did not see.
    """
    current = _device(device_id)
    data = _request(
        "PUT",
        f"/config/devices/{device_id}/desired",
        {"expected_generation": current["generation"], "settings": settings},
    )
    if not data.get("ok"):
        raise VoiceControlError(str(data.get("error") or "the Gateway refused the change"))
    verified = _device(device_id)
    if verified["generation"] != current["generation"] + 1:
        raise VoiceControlError(
            f"asked to advance from generation {current['generation']} but the device reports "
            f"{verified['generation']}"
        )
    # Desired state is recorded even when the device is away, so report which of the two happened
    # rather than implying the device already changed.
    return {"device": verified, "dispatched": bool(data.get("dispatched")), "accepted": True}


def _set_fleet(settings: dict, device_ids: Optional[list] = None) -> dict:
    """Write the same desired patch to every registered device (or a named subset).

    The fleet endpoint is one atomic write, so there is no per-device generation to guard; the
    Gateway bumps every targeted device by one and returns each record. Reading the fleet back and
    confirming every targeted generation advanced is how this tool proves the write landed rather
    than asserting it.
    """
    before = {d["device_id"]: d["generation"] for d in _fleet()["devices"]}
    payload: dict = {"settings": settings}
    if device_ids is not None:
        payload["device_ids"] = list(device_ids)
    data = _request("PUT", "/config/fleet/desired", payload)
    if not data.get("ok"):
        raise VoiceControlError(str(data.get("error") or "the Gateway refused the fleet change"))
    targets = list(device_ids) if device_ids is not None else list(before)
    after = {d["device_id"]: d["generation"] for d in _fleet()["devices"]}
    for device_id in targets:
        if device_id not in before:
            raise VoiceControlError(f"{device_id} is not a registered satellite")
        if after.get(device_id) != before[device_id] + 1:
            raise VoiceControlError(
                f"asked to advance {device_id} from generation {before[device_id]} but the "
                f"Gateway reports {after.get(device_id)}")
    return {"count": data.get("count", len(targets)),
            "devices": data.get("devices", []),
            "restart_pacing": bool(data.get("restart_pacing"))}


def handle(args: dict) -> str:
    args = args or {}
    action = str(args.get("action") or "").strip()
    if action not in _ACTIONS:
        return json.dumps({"ok": False,
                           "error": f"unknown action {action!r}; expected one of {list(_ACTIONS)}"},
                          ensure_ascii=False)
    try:
        if action == "fleet_status":
            data = _fleet()
            return json.dumps({"ok": True, "action": action, "count": data["count"],
                               "devices": data["devices"], "verified": True}, ensure_ascii=False)
        if action == "config_status":
            device_id = str(args.get("device_id") or "").strip()
            if not device_id:
                raise VoiceControlError("config_status needs a device_id")
            return json.dumps({"ok": True, "action": action, "device": _device(device_id),
                               "verified": True}, ensure_ascii=False)
        if action == "config_set":
            device_id = str(args.get("device_id") or "").strip()
            settings = args.get("settings")
            if not device_id:
                raise VoiceControlError("config_set needs a device_id")
            if not isinstance(settings, dict) or not settings:
                raise VoiceControlError("config_set needs a non-empty settings object")
            unknown = sorted(name for name in settings if name not in _SETTINGS)
            if unknown:
                raise VoiceControlError(
                    f"{', '.join(unknown)} is not a configurable setting; expected one of "
                    f"{list(_SETTINGS)}"
                )
            result = _set_config(device_id, settings)
            device = result["device"]
            return json.dumps({
                "ok": True, "action": action, "device_id": device_id,
                "generation": device["generation"], "desired": device["desired"],
                "dispatched": result["dispatched"], "converged": device["converged"],
                "drift": device["drift"], "verified": True,
            }, ensure_ascii=False)
        if action == "fleet_config_set":
            settings = args.get("settings")
            if not isinstance(settings, dict) or not settings:
                raise VoiceControlError("fleet_config_set needs a non-empty settings object")
            unknown = sorted(name for name in settings if name not in _SETTINGS)
            if unknown:
                raise VoiceControlError(
                    f"{', '.join(unknown)} is not a configurable setting; expected one of "
                    f"{list(_SETTINGS)}"
                )
            device_ids = args.get("device_ids")
            if device_ids is not None and (
                not isinstance(device_ids, list) or not all(isinstance(i, str) for i in device_ids)
            ):
                raise VoiceControlError("device_ids must be a list of strings")
            result = _set_fleet(settings, device_ids)
            return json.dumps({
                "ok": True, "action": action, "count": result["count"],
                "restart_pacing": result["restart_pacing"],
                "devices": result["devices"], "verified": True,
            }, ensure_ascii=False)
        if action == "conversation_reset":
            # Start a fresh conversation on a CONFIRMED request. Without this the model could ask
            # "did you want me to start a new conversation?" and then have nothing to call when the
            # person said yes -- which is exactly what happened on 2026-10-30 ("I can't reset the
            # conversation from this side").
            data = _request("POST", "/settings", {"conversation_reset": True})
            if not data.get("ok"):
                raise VoiceControlError(str(data.get("error") or "the Gateway refused the reset"))
            return json.dumps({"ok": True, "action": action,
                               "conversation": data.get("conversation"), "verified": True},
                              ensure_ascii=False)
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
