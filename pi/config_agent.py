"""Device-side half of the declarative voice-satellite configuration plane.

The agent accepts only a small versioned allowlist, persists the last confirmed
actual state atomically, and returns a Wyoming configuration report. It has no
shell or arbitrary-path surface; hardware-specific application is injected by
the installer for that device class.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from wyoming.event import Event

SCHEMA_VERSION = 1
CONFIG_DESIRED_EVENT = "hermes.config.desired"
CONFIG_REPORTED_EVENT = "hermes.config.reported"
_DEVICE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_WAKE_WORD = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Kept deliberately small and identical to bridge.config_plane schema v1.
SUPPORTED: dict[str, dict[str, Any]] = {
    "wake_word": {"type": "string", "pattern": _WAKE_WORD},
    "wake_threshold": {"type": "number", "minimum": 0.05, "maximum": 0.99},
    "mic_gain": {"type": "integer", "minimum": 0, "maximum": 100},
    "playback_volume": {"type": "integer", "minimum": 0, "maximum": 100},
    "led_brightness": {"type": "integer", "minimum": 0, "maximum": 100},
    "locale": {"type": "string", "enum": ["en-US"]},
    "update_channel": {"type": "string", "enum": ["stable", "beta"]},
}

ApplyCallback = Callable[[dict[str, Any]], Awaitable[tuple[dict, str, Optional[str]]] | tuple[dict, str, Optional[str]]]


class DeviceConfigError(ValueError):
    pass


def _validate_settings(values: Any, *, allow_empty: bool = False) -> dict[str, Any]:
    if not isinstance(values, dict) or (not values and not allow_empty):
        raise DeviceConfigError("settings must be a non-empty object")
    clean: dict[str, Any] = {}
    for name, value in values.items():
        spec = SUPPORTED.get(name)
        if spec is None:
            raise DeviceConfigError(f"unknown setting: {name}")
        kind = spec["type"]
        if kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise DeviceConfigError(f"{name} must be an integer")
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DeviceConfigError(f"{name} must be a number")
            value = float(value)
        elif kind == "string":
            if not isinstance(value, str):
                raise DeviceConfigError(f"{name} must be a string")
        if "minimum" in spec and value < spec["minimum"]:
            raise DeviceConfigError(f"{name} below minimum")
        if "maximum" in spec and value > spec["maximum"]:
            raise DeviceConfigError(f"{name} above maximum")
        if "enum" in spec and value not in spec["enum"]:
            raise DeviceConfigError(f"{name} is not supported")
        if name == "wake_word" and (not isinstance(value, str) or not _WAKE_WORD.fullmatch(value)):
            raise DeviceConfigError("wake_word contains unsupported characters")
        clean[name] = value
    return clean


class DeviceConfigAgent:
    def __init__(self, path: Path, *, apply: Optional[ApplyCallback] = None,
                 device_id: str = "") -> None:
        self.path = Path(path)
        self.apply = apply or self._stage_only
        self.state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "device_id": str(device_id or "").strip().lower(),
            "generation": 0,
            "desired": {},
            "actual": {},
            "health": "unknown",
            "error": None,
        }
        self._load()

    @staticmethod
    def _stage_only(_settings: dict[str, Any]) -> tuple[dict, str, Optional[str]]:
        return {}, "degraded", "desired state is staged; no hardware adapter is installed"

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            raise DeviceConfigError(f"could not read device configuration: {exc}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise DeviceConfigError("unsupported device configuration schema")
        self.state.update(data)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(self.state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def current_report(self, *, health: Optional[str] = None, error: Optional[str] = None) -> Event:
        return Event(type=CONFIG_REPORTED_EVENT, data={
            "generation": int(self.state.get("generation", 0)),
            "actual": dict(self.state.get("actual") or {}),
            "health": health if health is not None else self.state.get("health", "unknown"),
            "error": error if error is not None else self.state.get("error"),
        })

    async def apply_event(self, event: Event) -> Event:
        data = dict(event.data or {})
        try:
            if event.type != CONFIG_DESIRED_EVENT:
                raise DeviceConfigError("not a desired-state event")
            if data.get("schema_version") != SCHEMA_VERSION:
                raise DeviceConfigError("unsupported configuration schema")
            device_id = str(data.get("device_id") or "").strip().lower()
            if not _DEVICE_ID.fullmatch(device_id):
                raise DeviceConfigError("invalid device_id")
            current_id = str(self.state.get("device_id") or "")
            if current_id and device_id != current_id:
                raise DeviceConfigError("desired state belongs to another device")
            generation = data.get("generation")
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
                raise DeviceConfigError("generation must be a positive integer")
            if generation <= int(self.state.get("generation", 0)):
                return self.current_report()
            settings = _validate_settings(data.get("settings"))
        except DeviceConfigError as exc:
            return self.current_report(health="error", error=str(exc))

        try:
            result = self.apply(settings)
            if inspect.isawaitable(result):
                result = await result
            actual, health, error = result
            actual = _validate_settings(actual, allow_empty=True)
            if health not in {"healthy", "degraded", "error"}:
                raise DeviceConfigError("apply callback returned an invalid health")
            if error is not None and not isinstance(error, str):
                raise DeviceConfigError("apply callback returned an invalid error")
        except Exception as exc:  # noqa: BLE001 - application failure becomes reported state
            return self.current_report(health="error", error=f"apply failed: {exc}")

        self.state.update({
            "device_id": device_id,
            "generation": generation,
            "desired": settings,
            "actual": actual,
            "health": health,
            "error": error,
        })
        self._save()
        return self.current_report()


__all__ = [
    "CONFIG_DESIRED_EVENT", "CONFIG_REPORTED_EVENT", "DeviceConfigAgent",
    "DeviceConfigError", "SCHEMA_VERSION", "SUPPORTED",
]
