"""Versioned declarative configuration state for voice-satellite fleets.

The plane stores only allowlisted, non-secret settings. It never accepts shell
commands or arbitrary paths. Devices apply desired state separately and report
actual state back with the generation they observed.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1
_DEVICE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_WAKE_WORD = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

DEVICE_SETTING_SCHEMA: dict[str, dict[str, Any]] = {
    "voice": {
        "type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
        "default": "default", "apply": "immediate", "gateway_owned": True,
    },
    # -- capability, not a setting ------------------------------------------------------------
    # The rate a device's own audio path runs at, so the gateway can send it audio it can play
    # as-is. Reported by the device, NEVER sent as desired state: a Pi satellite would report it
    # as unapplied and go degraded, and there is nothing for it to apply.
    "snd_rate": {
        "type": "integer", "minimum": 8000, "maximum": 96000,
        "default": 22050, "apply": "immediate", "capability": True,
    },
    "wake_word": {
        "type": "string", "pattern": _WAKE_WORD.pattern,
        "default": "hey_hermes", "apply": "wake_service_restart",
    },
    "wake_threshold": {
        "type": "number", "minimum": 0.05, "maximum": 0.99,
        "default": 0.35, "apply": "wake_service_restart",
    },
    "mic_volume": {
        # How loudly this satellite hears you. The schema names the intent; whether a device turns it
        # into microphone gain, a DSP control, or nothing at all (a fixed-gain HAT) is the device's
        # own configuration, and it reports back the level it actually runs at.
        "type": "integer", "minimum": 0, "maximum": 100,
        "default": 78, "apply": "immediate",
    },
    "playback_volume": {
        "type": "integer", "minimum": 0, "maximum": 100,
        "default": 55, "apply": "immediate",
        # The Gateway speaks a user scale (1-100 over what the hardware can do) while the
        # device sets an integer ALSA percentage, so the round trip user->raw->user shifts
        # by one for 49 of the 100 values. Without a tolerance those settings would report
        # permanent drift and `converged: false` forever, which is worse than useless: it
        # teaches whoever reads the fleet view to ignore drift altogether.
        # The setting IS converged, at the resolution the hardware can express.
        "round_trip_tolerance": 1,
    },
    "led_brightness": {
        # The LED driver takes 1-31 (APA102 global brightness is 5 bits and the service's
        # --led-brightness refuses anything else). The old 0-100 range with a default of 40
        # described a scale the hardware does not have: 40 would never converge and 0 would
        # not mean "off", because the strip's power rail is a GPIO, not a level.
        "type": "integer", "minimum": 1, "maximum": 31,
        "default": 31, "apply": "led_service",
    },
}


class ConfigValidationError(ValueError):
    """A desired/reported setting does not satisfy the public schema."""


class ConfigConflict(RuntimeError):
    """The writer supplied a stale desired-state generation."""


#: The settings a device can apply without bouncing one of its own services. Everything else
#: is restart-class: applying it restarts the wake engine or the LED service, which on this
#: hardware means the audio stack goes quiet for a moment. A fleet write that touches any of
#: these must be paced so the rooms do not all go silent at once.
IMMEDIATE_APPLY = frozenset({"immediate"})


def setting_requires_restart(name: str) -> bool:
    """Whether applying a setting bounces a device service, from the schema's `apply` field."""
    return DEVICE_SETTING_SCHEMA.get(name, {}).get("apply") not in IMMEDIATE_APPLY


def values_agree(name: str, wanted: Any, reported: Any) -> bool:
    """Whether a reported value counts as the desired one.

    Exact for everything except settings the hardware round-trips through a coarser scale,
    where a whole-number difference of one is the same instruction arriving back.
    """
    if reported is None:
        return False
    tolerance = DEVICE_SETTING_SCHEMA.get(name, {}).get("round_trip_tolerance", 0)
    if tolerance and isinstance(wanted, (int, float)) and isinstance(reported, (int, float)):
        return not isinstance(wanted, bool) and not isinstance(reported, bool) \
            and abs(wanted - reported) <= tolerance
    return wanted == reported


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_device_id(device_id: str) -> str:
    candidate = str(device_id or "").strip().lower()
    if not _DEVICE_ID.fullmatch(candidate):
        raise ConfigValidationError(
            "device_id must be 1-64 lowercase letters, digits, dots, dashes, or underscores"
        )
    return candidate


def validate_settings(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ConfigValidationError("settings must be a JSON object")
    clean: dict[str, Any] = {}
    for name, value in values.items():
        spec = DEVICE_SETTING_SCHEMA.get(name)
        if spec is None:
            raise ConfigValidationError(f"unknown device setting: {name}")
        kind = spec["type"]
        if kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigValidationError(f"{name} must be an integer")
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigValidationError(f"{name} must be a number")
            value = float(value)
        elif kind == "string":
            if not isinstance(value, str):
                raise ConfigValidationError(f"{name} must be a string")
        else:  # pragma: no cover - schema author error
            raise RuntimeError(f"unsupported schema type {kind}")
        if "minimum" in spec and value < spec["minimum"]:
            raise ConfigValidationError(f"{name} must be at least {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            raise ConfigValidationError(f"{name} must be at most {spec['maximum']}")
        if "enum" in spec and value not in spec["enum"]:
            raise ConfigValidationError(f"{name} must be one of {spec['enum']}")
        if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
            raise ConfigValidationError(f"{name} contains unsupported characters")
        if name == "wake_word" and not _WAKE_WORD.fullmatch(value):
            raise ConfigValidationError("wake_word contains unsupported characters")
        clean[name] = value
    return clean


class ConfigPlane:
    """Atomic desired/reported state with optimistic generation checks."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._pending_save = False
        self._state = self._load()
        # _load() may have migrated a legacy profile but cannot save it yet: `self._state`
        # does not exist until this line. Persist the migration now, before the plane is used.
        if self._pending_save:
            self._save(self._state)
            self._pending_save = False

    def voice_for(self, device_id: str | None) -> str | None:
        record = self._state["devices"].get(device_id, {})
        voice = (record.get("desired") or {}).get("voice")
        return None if voice in (None, "default") else voice

    @staticmethod
    def device_settings(record: dict) -> dict:
        return {key: value for key, value in record["desired"].items()
                if not DEVICE_SETTING_SCHEMA.get(key, {}).get("gateway_owned")}

    def schema(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "device_settings": DEVICE_SETTING_SCHEMA,
            "persistence": "atomic JSON, mode 0600",
        }

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "devices": {}}

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return self._empty()
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigValidationError(f"could not read configuration state: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise ConfigValidationError("unsupported configuration-state schema")
        if not isinstance(raw.get("devices"), dict):
            raise ConfigValidationError("configuration state has no devices object")
        if self._migrate_legacy(raw):
            # Persist before this becomes the active state: the migrated profile must be on
            # disk so the first reader after a restart sees values already in range.
            self._pending_save = True
        return raw

    @staticmethod
    def _migrate_legacy(state: dict[str, Any]) -> bool:
        """Migrate out-of-range legacy `led_brightness` values into the current schema.

        Before the schema was corrected to 1-31, the default was 40 on a 0-100 scale that
        the APA102 LED driver does not have: such a value could never converge, and the
        device's LED service was actually running at its own default (31). A device enrolled
        under the old schema therefore still carries an out-of-range desired `led_brightness`.
        Because that value was never in force, the honest migration is to the level the
        hardware actually ran (31), and the generation is advanced so the corrected profile
        is re-delivered to the device. Without this, the new device-side bounds would reject
        every delivery for that device - even an unrelated volume change - on the strength of
        the poisoned legacy value.
        """
        changed = False
        for record in (state.get("devices") or {}).values():
            # Records written before the schema named intents instead of mechanisms carry keys the
            # schema no longer has: `locale` and `update_channel` (nothing ever consumed them) and
            # `mic_gain` (now `mic_volume`). Left in place they fail validation, so the device reads
            # as `error` -- a worse lie than the `degraded` those keys caused in the first place.
            # The rename keeps the value: the level in force does not change, only its name.
            stale_keys = ("locale", "update_channel")
            renamed = False
            for where in ("desired", "reported"):
                values = record.get(where)
                if not isinstance(values, dict):
                    continue
                for stale in stale_keys:
                    if stale in values:
                        values.pop(stale)
                        changed = True
                if "mic_gain" in values:
                    if "mic_volume" not in values:
                        values["mic_volume"] = values["mic_gain"]
                        renamed = True
                    values.pop("mic_gain")
                    changed = True
            if renamed and isinstance(record.get("desired"), dict):
                # Re-deliver the corrected profile, so the device converges on the new key name
                # rather than waiting for somebody to change an unrelated setting.
                record["generation"] = int(record.get("generation", 0)) + 1
                record["desired_updated_at"] = _utc_now()

            desired = record.get("desired")
            if not isinstance(desired, dict):
                continue
            led = desired.get("led_brightness")
            if isinstance(led, bool) or not isinstance(led, int):
                continue
            if led < 1 or led > 31:
                desired["led_brightness"] = 31
                record["generation"] = int(record.get("generation", 0)) + 1
                record["desired_updated_at"] = _utc_now()
                changed = True
        return changed

    def _save(self, state: Optional[dict[str, Any]] = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            # mkstemp already creates the file as mode 0600; fchmod is best-effort.
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "w") as handle:
                json.dump(self._state if state is None else state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
            # mkstemp already created the file as mode 0600. The post-replace chmod is a
            # best-effort hardening step for systems where replace preserves the old inode's mode;
            # a chmod failure must never roll back a committed generation.
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _record(
        self, device_id: str, *, state: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        device_id = _validate_device_id(device_id)
        devices = (self._state if state is None else state)["devices"]
        return devices.setdefault(device_id, {
            "device_id": device_id,
            "metadata": {},
            "generation": 0,
            "desired": {},
            "reported_generation": None,
            "reported": {},
            "health": "unknown",
            "last_error": None,
            "desired_updated_at": None,
            "reported_at": None,
        })

    def ensure_device(
        self, device_id: str, *, metadata: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Create a newly enrolled device with the complete safe default profile.

        The wake word default is `hey_hermes` on every platform, because that is the model both
        installers provision: the ESP32's bundled default (firmware/esp32/models/) and the Pi's
        installer model (scripts/install-pi.sh). A desired value whose model is absent reads as a
        permanent degraded report, so the default has to be the model that is actually on the
        device. `set_desired` remains the way to move a device to any other word once its model is
        deployed (for example an Apache-2.0 model in a commercial deployment).
        """
        candidate = deepcopy(self._state)
        record = self._record(device_id, state=candidate)
        clean_metadata: dict[str, Any] = {}
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise ConfigValidationError("metadata must be a JSON object")
            for name in ("name", "area", "firmware_version", "boot_id", "profile_id"):
                value = metadata.get(name)
                if value is not None:
                    if not isinstance(value, str) or len(value) > 128:
                        raise ConfigValidationError(f"metadata {name} must be a short string")
                    clean_metadata[name] = value
            capabilities = metadata.get("capabilities")
            if capabilities is not None:
                if (not isinstance(capabilities, list)
                        or len(capabilities) > 32
                        or not all(isinstance(item, str) and len(item) <= 32
                                   for item in capabilities)):
                    raise ConfigValidationError("metadata capabilities must be a short string list")
                clean_metadata["capabilities"] = list(capabilities)

        changed = record.get("metadata") != clean_metadata
        record["metadata"] = clean_metadata
        if int(record["generation"]) == 0:
            record["desired"] = {
                name: spec["default"] for name, spec in DEVICE_SETTING_SCHEMA.items()
                if not spec.get("capability")
            }
            record["generation"] = 1
            record["desired_updated_at"] = _utc_now()
            changed = True
        if changed:
            self._save(candidate)
            self._state = candidate
        return self._public(record)

    def set_desired(
        self,
        device_id: str,
        patch: dict[str, Any],
        *,
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        clean = validate_settings(patch)
        if not clean:
            raise ConfigValidationError("at least one setting is required")
        candidate = deepcopy(self._state)
        record = self._record(device_id, state=candidate)
        current = int(record["generation"])
        if expected_generation is not None and expected_generation != current:
            raise ConfigConflict(
                f"expected generation {expected_generation}, current generation is {current}"
            )
        desired = dict(record["desired"])
        desired.update(clean)
        record["desired"] = desired
        record["generation"] = current + 1
        record["desired_updated_at"] = _utc_now()
        self._save(candidate)
        self._state = candidate
        return self._public(record)

    def set_desired_fleet(
        self,
        patch: dict[str, Any],
        *,
        device_ids: Optional[Iterable[str]] = None,
    ) -> list[dict[str, Any]]:
        """Apply one desired-state patch to every device, durably, in a single save.

        `device_ids` narrows the target set (a subset write). `None` means every device in
        the fleet. Each targeted device's generation is bumped exactly once and its desired
        state merged with the patch, mirroring `set_desired`. The write is one atomic save,
        so a crash cannot leave the fleet half-updated. Devices not targeted are untouched.

        The optimistic-concurrency guard is deliberately not per-device here: a fleet write is a
        single operator intent ("make it so everywhere"), and re-reading every device to carry
        N generations through the client defeats the point of the endpoint. The guard is
        still enforced on the single-device path, which is where concurrent writers actually
        race. A fleet write observed mid-flight by a single-device writer will 409, which is
        the correct, conservative answer.
        """
        clean = validate_settings(patch)
        if not clean:
            raise ConfigValidationError("at least one setting is required")
        candidate = deepcopy(self._state)
        devices = candidate["devices"]
        if device_ids is None:
            targets = set(devices)
        else:
            targets = set()
            for item in device_ids:
                targets.add(_validate_device_id(item))
            unknown = targets - set(devices)
            if unknown:
                raise ConfigValidationError(
                    "unknown device(s) in fleet target: "
                    + ", ".join(sorted(unknown))
                )
        if not targets:
            raise ConfigValidationError("fleet target has no devices")
        now = _utc_now()
        for device_id in sorted(targets):
            record = devices[device_id]
            desired = dict(record.get("desired") or {})
            desired.update(clean)
            record["desired"] = desired
            record["generation"] = int(record["generation"]) + 1
            record["desired_updated_at"] = now
        self._save(candidate)
        self._state = candidate
        return [
            self._public(candidate["devices"][device_id]) for device_id in sorted(targets)
        ]

    def report(
        self,
        device_id: str,
        *,
        generation: int,
        actual: dict[str, Any],
        health: str,
        error: Optional[str],
    ) -> dict[str, Any]:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ConfigValidationError("reported generation must be a non-negative integer")
        clean = validate_settings(actual)
        if health not in {"healthy", "degraded", "error", "unknown"}:
            raise ConfigValidationError("health must be healthy, degraded, error, or unknown")
        if error is not None and (not isinstance(error, str) or len(error) > 500):
            raise ConfigValidationError("error must be null or a string no longer than 500 characters")
        candidate = deepcopy(self._state)
        record = self._record(device_id, state=candidate)
        record.update({
            "reported_generation": generation,
            "reported": clean,
            "health": health,
            "last_error": error,
            "reported_at": _utc_now(),
        })
        # A report is accepted only once it is on disk, for the same reason as set_desired:
        # otherwise a failed save leaves the fleet view claiming a health that the disk never
        # saw, and the next reader of the file disagrees with the screen.
        self._save(candidate)
        self._state = candidate
        return self._public(record)

    def get_device(self, device_id: str, *, connected: Optional[bool] = None) -> dict[str, Any]:
        device_id = _validate_device_id(device_id)
        record = self._state["devices"].get(device_id)
        if record is None:
            raise KeyError(device_id)
        return self._public(record, connected=connected)

    def list_devices(self, *, connected_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
        connected = {_validate_device_id(item) for item in connected_ids}
        return [
            self._public(self._state["devices"][device_id], connected=device_id in connected)
            for device_id in sorted(self._state["devices"])
        ]

    @staticmethod
    def _public(record: dict[str, Any], *, connected: Optional[bool] = None) -> dict[str, Any]:
        desired = dict(record.get("desired") or {})
        reported = dict(record.get("reported") or {})
        gateway_reported = {name: wanted for name, wanted in desired.items()
                            if DEVICE_SETTING_SCHEMA.get(name, {}).get("gateway_owned")}
        effective_reported = {**reported, **gateway_reported}
        drift = {
            name: {"desired": wanted, "reported": effective_reported.get(name)}
            for name, wanted in desired.items()
            if not values_agree(name, wanted, effective_reported.get(name))
        }
        result = {
            **record,
            "desired": desired,
            "reported": reported,
            "gateway_reported": gateway_reported,
            "drift": drift,
            "converged": (
                record.get("reported_generation") == record.get("generation")
                and not drift
                and record.get("health") == "healthy"
            ),
        }
        if connected is not None:
            result["connected"] = bool(connected)
        return result


__all__ = [
    "SCHEMA_VERSION", "DEVICE_SETTING_SCHEMA", "ConfigPlane",
    "ConfigValidationError", "ConfigConflict", "validate_settings",
    "IMMEDIATE_APPLY", "setting_requires_restart",
]
