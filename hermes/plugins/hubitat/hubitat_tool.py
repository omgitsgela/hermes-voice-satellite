"""Hubitat Maker API access for Hermes, as a plugin tool.

Why this exists as a plugin tool rather than reusing the documented helper script: the helper
is a CLI, so it needs the `terminal` toolset, which the voice surface deliberately lacks. That
is precisely why a spoken "what's the porch temperature?" failed on 2026-09-22 — the agent read
the Hubitat skill, learned it had to run `hubitat-control.py`, and had no way to run it.

Safety model. Rather than inventing rules, this enforces Angela's OWN policy from
`~/.openclaw/secrets/hubitat-maker-api.json`:

* `risk_policy.forbidden_commands` (lock-code operations) are refused outright, always.
* Devices whose capabilities include `Lock`/`LockCodes`, or whose label contains a
  `risky_label_words` entry, are refused for every COMMAND. Reads stay allowed, because her
  policy treats reads as safe.
* On top of that blacklist, a WHITELIST of known-safe commands is enforced. A microphone in a
  shared living space wants the opposite default: anything not known-safe is refused.
* There is deliberately **no `--allow-risky` equivalent**. Her CLI can escalate a risky action
  after explicit confirmation by a human; a voice channel cannot obtain confirmation, so it
  must never be able to unlock a door or touch a lock code.

State changes are verified by reading the device back, retrying briefly because Maker API
reads can lag, per the rule in the `angela-hubitat` skill.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

DEFAULT_CONFIG = "~/.openclaw/secrets/hubitat-maker-api.json"

# Commands this tool will ever send. Everything else is refused, whatever the policy says.
# NOTE for the colour actions, learned the hard way on 2026-09-22: on Angela's Govee cloud
# lights ("Govee v2 Color Lights..." drivers) the hub ACCEPTS setHue, setSaturation, setEffect and
# setNextEffect, answers no error, and the light does not change. on, off, setLevel and
# setColorTemperature do work there, and the hub's own attributes track those. Colour works on the
# Zigbee colour lights. So a colour request aimed at a Govee light should be answered honestly
# rather than promised. The driver side of this is recorded in the project note, not fixed here.
SAFE_COMMANDS = {
    "on", "off", "toggle", "refresh",
    "setLevel", "setColorTemperature", "setColor",
    # Colour lighting, added 2026-09-22 after a spoken "set fairy lights to red" was refused.
    # The Govee drivers take colour as a COLOR_MAP for setColor, which the Maker API command path
    # cannot carry, so hue and saturation are sent as separate numeric commands instead. On these
    # drivers setEffect takes a NUMBER, not a name.
    "setHue", "setSaturation", "setEffect", "setNextEffect", "setPreviousEffect",
    "setCoolingSetpoint", "setHeatingSetpoint", "setThermostatMode", "setThermostatFanMode",
}

# Attributes worth reporting back for common device types.
INTERESTING_ATTRIBUTES = (
    "switch", "level", "temperature", "humidity", "thermostatMode", "coolingSetpoint",
    "heatingSetpoint", "thermostatOperatingState", "motion", "contact", "presence",
    "battery", "power", "colorTemperature",
    # Colour lights: without these a colour change cannot be reported or verified.
    "colorMode", "hue", "saturation", "colorName", "effectName", "effectNum",
)

# Read-only Maker API commands, used only to decide whether a command mutates state.
_READ_ONLY = {"refresh"}


class HubitatError(RuntimeError):
    pass


def config_path() -> str:
    return os.path.expanduser(os.environ.get("HUBITAT_MAKER_API_JSON", DEFAULT_CONFIG))


def load_config() -> dict:
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError as exc:
        raise HubitatError(f"Hubitat Maker API config not found at {path}") from exc
    for key in ("hubitat_base_url", "app_id", "access_token"):
        if not cfg.get(key):
            raise HubitatError(f"Hubitat config at {path} is missing {key}")
    return cfg


def api_url(cfg: dict, path: str) -> str:
    base = str(cfg["hubitat_base_url"]).rstrip("/")
    app_id = urllib.parse.quote(str(cfg["app_id"]))
    token = urllib.parse.quote(str(cfg["access_token"]))
    sep = "&" if "?" in path else "?"
    # The token is quoted and never logged, returned, or included in any error message.
    return f"{base}/apps/api/{app_id}/{path.lstrip('/')}{sep}access_token={token}"


def _get_json(cfg: dict, path: str, timeout: float = 20.0) -> Any:
    url = api_url(cfg, path)
    safe_label = path.split("?")[0]
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        # A 5xx on a command path has (at least) two common causes, and an earlier version of this
        # message asserted only the first — wrongly, on an outlet that was perfectly healthy. Keep
        # both possibilities visible rather than being confidently wrong again.
        #
        # NOTE: use getattr, not isinstance(urllib.error.HTTPError). A local `import urllib.error`
        # inside this function makes `urllib` a LOCAL name for the WHOLE function, shadowing the
        # module-level `import urllib.request` and breaking the urlopen call above with
        # UnboundLocalError. That exact mistake broke every call in this module once.
        status = getattr(exc, "code", None)
        if isinstance(status, int) and status >= 500:
            raise HubitatError(
                f"the hub returned a server error (HTTP {status}) for {safe_label}. That can mean "
                f"the request shape is wrong (verify the path form), that the device is unreachable "
                f"on its mesh, or that its driver threw. Check the device in the Hubitat UI before "
                f"concluding anything about the hardware.") from exc
        raise HubitatError(f"Maker API request failed for {safe_label}: "
                           f"{type(exc).__name__}: {exc}") from exc
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body.strip()[:500]}


# --- device helpers ---------------------------------------------------------

def all_devices(cfg: dict) -> list[dict]:
    devices = _get_json(cfg, "devices")
    return devices if isinstance(devices, list) else []


def device_detail(cfg: dict, device_id: str) -> dict:
    detail = _get_json(cfg, f"devices/{urllib.parse.quote(str(device_id))}")
    if not isinstance(detail, dict):
        raise HubitatError(f"unexpected device detail for id {device_id}")
    return detail


def capability_names(device: dict) -> list[str]:
    """Capability names, tolerating both shapes.

    The Maker API returns `capabilities` as a list of PLAIN STRINGS (e.g. "Lock",
    "Configuration"), not objects. An earlier version assumed dicts and silently produced
    [""], which would have defeated the capability half of the lock guard and left only the
    label-word check: a lock named without the word "lock" would have looked ordinary.
    """
    names: set[str] = set()
    for cap in device.get("capabilities") or []:
        if isinstance(cap, str):
            names.add(cap)
        elif isinstance(cap, dict):
            name = cap.get("name") or cap.get("capability")
            if name:
                names.add(str(name))
    return sorted(names)


def attributes(device: dict) -> dict:
    out = {}
    for attr in device.get("attributes") or []:
        if isinstance(attr, dict) and attr.get("name"):
            out[str(attr["name"])] = attr.get("currentValue")
    return out


def display_name(device: dict) -> str:
    label = str(device.get("label") or "").strip()
    name = str(device.get("name") or "").strip()
    return label or name or f"device {device.get('id')}"


def find_device(devices: list[dict], query: str) -> Optional[dict]:
    """Exact id, then case-insensitive exact label, then unique substring."""
    q = (query or "").strip().lower()
    if not q:
        return None
    for device in devices:
        if str(device.get("id", "")) == q:
            return device
    for device in devices:
        if display_name(device).lower() == q:
            return device
    matches = [d for d in devices if q in display_name(d).lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(display_name(d) for d in matches[:6])
        raise HubitatError(f"{len(matches)} devices match {query!r}: {names}")
    return None


# --- policy -----------------------------------------------------------------

def forbidden_commands(cfg: dict) -> set[str]:
    policy = cfg.get("risk_policy") or {}
    builtin = {"setCode", "deleteCode", "setCodeLength", "getCodes"}
    return builtin | {str(c) for c in (policy.get("forbidden_commands") or [])}


def risk_reason(cfg: dict, device: dict) -> Optional[str]:
    """Why this device must not be commanded, or None when it is ordinary."""
    policy = cfg.get("risk_policy") or {}
    risky_caps = {str(c) for c in (policy.get("risky_capabilities") or [])}
    hits = risky_caps.intersection(capability_names(device))
    if hits:
        return f"capability {sorted(hits)}"
    label = display_name(device).lower()
    for word in policy.get("risky_label_words") or []:
        if str(word).lower() in label:
            return f"label contains {word!r}"
    return None


def check_command_allowed(cfg: dict, device: dict, command: str) -> None:
    if command in forbidden_commands(cfg):
        raise HubitatError(
            f"command {command!r} is forbidden by Angela's Hubitat policy and is never sent")
    if command not in SAFE_COMMANDS:
        raise HubitatError(
            f"command {command!r} is not in this tool's safe list; refusing. "
            f"Allowed: {sorted(SAFE_COMMANDS)}")
    if command in _READ_ONLY:
        return
    reason = risk_reason(cfg, device)
    if reason:
        raise HubitatError(
            f"{display_name(device)!r} is a risky device ({reason}) and this channel cannot "
            f"obtain the confirmation Angela's policy requires, so it will not be operated. "
            f"Reads are still allowed.")


# --- reporting --------------------------------------------------------------

def summarise(device: dict, *, detail: bool = False) -> dict:
    attrs = attributes(device)
    units = {str(a["name"]): a.get("unit")
             for a in (device.get("attributes") or [])
             if isinstance(a, dict) and a.get("name")}
    picked = {k: v for k, v in attrs.items()
              if k in INTERESTING_ATTRIBUTES and v not in (None, "")}
    out = {
        "id": device.get("id"),
        "name": display_name(device),
        "label": device.get("label"),
        "attributes": picked,
    }
    if "temperature" in picked:
        out["temperature"] = picked["temperature"]
        out["temperature_unit"] = units.get("temperature") or ""
    if detail:
        out["capabilities"] = capability_names(device)
        risk = risk_reason(load_config(), device)
        out["risky"] = bool(risk)
        if risk:
            out["risky_reason"] = risk
    return out


def hue_of_rgb(rgb: Any) -> Optional[float]:
    """Hue in degrees from a 24-bit RGB integer, or None for greyscale.

    This exists because the `hue` attribute on the Govee drivers cannot be trusted while
    `colorRGBNum` can: on 2026-09-22 the attribute said 20 while the cloud reported #ccff00, which
    is 72 degrees. Angela's own skill already said to judge these lights "from colorMode,
    colorTemperature, and the RGB values"; this is that rule, in code.
    """
    try:
        value = int(str(rgb).strip())
    except (TypeError, ValueError):
        return None
    r, g, b = (value >> 16) & 255, (value >> 8) & 255, value & 255
    highest, lowest = max(r, g, b), min(r, g, b)
    if highest == lowest:
        return None
    delta = highest - lowest
    if highest == r:
        hue = 60 * (((g - b) / delta) % 6)
    elif highest == g:
        hue = 60 * ((b - r) / delta + 2)
    else:
        hue = 60 * ((r - g) / delta + 4)
    return hue % 360


def _refresh(cfg: dict, device_id: str) -> None:
    """Ask a driver to re-read its device. Cached attributes do not update on their own."""
    try:
        _get_json(cfg, f"devices/{urllib.parse.quote(str(device_id))}/refresh")
    except Exception:  # noqa: BLE001 - a failed refresh must not break a verification
        pass


def read_verified(cfg: dict, device_id: str, expect: Optional[tuple[str, Any]] = None,
                  attempts: int = 4, delay: float = 0.4, refresh_first: bool = False) -> dict:
    """Read a device back, retrying briefly. Maker API reads can lag a command.

    `refresh_first` matters for cloud lights. The Hubitat Govee drivers cache their attributes, and
    a change made through them does not show up on a plain read until the driver polls again. The
    `angela-hubitat` skill is explicit that a status read without a preceding refresh "returns
    confidently wrong values", which is exactly what made a colour change read as unverified on
    2026-09-22: the command had gone out, the hub just had not caught up.
    """
    if refresh_first:
        _refresh(cfg, device_id)
        time.sleep(1.2)
    detail = device_detail(cfg, device_id)
    if expect is None:
        return detail
    attr, want = expect
    for _ in range(max(1, attempts)):
        got = attributes(detail).get(attr)
        if str(got).lower() == str(want).lower():
            return detail
        time.sleep(delay)
        if refresh_first:
            _refresh(cfg, device_id)
        detail = device_detail(cfg, device_id)
    return detail


def _command_needs_verification(command: str) -> Optional[tuple[str, str]]:
    """(attribute, expected) to check after a command, or None when there is no single answer."""
    return {
        "on": ("switch", "on"),
        "off": ("switch", "off"),
    }.get(command)
