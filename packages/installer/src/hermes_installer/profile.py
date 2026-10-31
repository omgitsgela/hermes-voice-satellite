"""The device profile: the public contract that makes a device a file, not a fork.

A profile declares what a device is and what can be done to it. A driver implements three verbs
(flash, provision, verify) and nothing about the user interface. The installer renders from the
profile, so the capability badges a person sees come from the same data the code obeys.

This module is deliberately boring: dataclasses, explicit validation, and error messages that name
the field. A third-party profile is expected to fail loudly here rather than mysteriously in a UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: How the device's own software is written to it.
FLASHING_METHODS = ("usb_serial", "sd_card")

#: Wake-word formats a device can run. The phrase must exist in the device's format, and the two
#: are not interchangeable: a Pi runs openWakeWord ONNX, an ESP32 runs microWakeWord TFLite.
WAKE_FORMATS = ("openwakeword_onnx", "microwakeword_tflite")

#: How a setting reaches a running device.
APPLY_MODES = ("immediate", "service_restart", "next_turn", "cable")

#: What the device can speak through.
VOICE_CAPABILITIES = ("local_piper", "none")


@dataclass(frozen=True)
class Setting:
    """One configurable thing, and the honest answer about how it can be changed."""

    key: str
    apply: str
    #: The config plane carries this, so a paired installer can change it over the network.
    remote: bool
    help: str = ""

    def __post_init__(self) -> None:
        # A string "false" is truthy, so a profile could claim a cable-only setting was remote by
        # quoting the word. Require an actual boolean.
        if not isinstance(self.remote, bool):
            raise ValueError(f"{self.key}: remote must be true or false, not {self.remote!r}")
        if self.apply not in APPLY_MODES:
            raise ValueError(f"{self.key}: apply must be one of {APPLY_MODES}, not {self.apply!r}")
        if self.apply == "cable" and self.remote:
            raise ValueError(f"{self.key}: a cable-only setting cannot also be remote")
        if not self.key or not self.key.replace("_", "").isalnum():
            raise ValueError(f"invalid setting key {self.key!r}")


@dataclass(frozen=True)
class DeviceProfile:
    """Everything the installer needs to know about a device it did not have to be taught."""

    id: str
    display_name: str
    vendor: str
    flashing: str
    wake_formats: tuple[str, ...]
    voice: str
    settings: Mapping[str, Setting] = field(default_factory=dict)
    image: str | None = None
    description: str = ""
    wake_default: str = "hey_hermes"
    #: Device classes whose firmware is ours to ship, as opposed to a third party's.
    first_party: bool = False

    def __post_init__(self) -> None:
        if not self.id or not self.id.replace("-", "").isalnum():
            raise ValueError(f"invalid device id {self.id!r}")
        if self.flashing not in FLASHING_METHODS:
            raise ValueError(f"{self.id}: flashing must be one of {FLASHING_METHODS}")
        if self.voice not in VOICE_CAPABILITIES:
            raise ValueError(f"{self.id}: voice must be one of {VOICE_CAPABILITIES}")
        if not self.wake_formats:
            raise ValueError(f"{self.id}: at least one wake format is required")
        for fmt in self.wake_formats:
            if fmt not in WAKE_FORMATS:
                raise ValueError(f"{self.id}: unknown wake format {fmt!r}")
        # The mapping key and the setting's own key must agree, or the UI would label one setting
        # with another's capability. This replaces an earlier guard that compared a set with itself
        # and therefore could never fail.
        for key, setting in self.settings.items():
            if key != setting.key:
                raise ValueError(
                    f"{self.id}: setting registered as {key!r} calls itself {setting.key!r}"
                )

    # -- what the UI asks a profile ------------------------------------------------------------

    @property
    def remote_settings(self) -> tuple[str, ...]:
        """Settings a paired installer can change without touching the device."""
        return tuple(sorted(k for k, s in self.settings.items() if s.remote))

    @property
    def cable_settings(self) -> tuple[str, ...]:
        """Settings that need the device in hand (and on a Pi, the card out)."""
        return tuple(sorted(k for k, s in self.settings.items() if not s.remote))

    @property
    def needs_card_reader(self) -> bool:
        return self.flashing == "sd_card"

    def wake_catalogue_format(self) -> str:
        """Which catalogue this device's wake words come from."""
        return self.wake_formats[0]


def setting_from_dict(key: str, raw: Mapping[str, Any]) -> Setting:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{key}: a setting must be an object")
    try:
        return Setting(
            key=key,
            apply=raw["apply"],
            remote=bool(raw["remote"]),
            help=str(raw.get("help", "")),
        )
    except KeyError as exc:
        raise ValueError(f"{key}: missing required field {exc.args[0]!r}") from None


def profile_from_dict(raw: Mapping[str, Any]) -> DeviceProfile:
    """Build a profile from parsed JSON/YAML, naming the offending field on failure.

    Kept separate from the dataclass so a third-party profile file gets a message that says which
    key is wrong, instead of a TypeError from deep inside a constructor.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("a device profile must be an object")
    required = ("id", "display_name", "vendor", "flashing", "wake_formats", "voice")
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(f"device profile is missing {', '.join(missing)}")

    settings_raw = raw.get("settings") or {}
    if not isinstance(settings_raw, Mapping):
        raise ValueError("settings must be an object of key -> {apply, remote}")
    settings = {key: setting_from_dict(key, value) for key, value in settings_raw.items()}

    formats = raw["wake_formats"]
    if isinstance(formats, str):
        formats = [formats]

    return DeviceProfile(
        id=str(raw["id"]),
        display_name=str(raw["display_name"]),
        vendor=str(raw["vendor"]),
        flashing=str(raw["flashing"]),
        wake_formats=tuple(formats),
        voice=str(raw["voice"]),
        settings=settings,
        image=raw.get("image"),
        description=str(raw.get("description", "")),
        wake_default=str(raw.get("wake_default", "hey_hermes")),
        first_party=bool(raw.get("first_party", False)),
    )
