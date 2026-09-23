"""Hubitat plugin — registers the ``hubitat`` tool.

Split like the geolocation plugin: registration here, logic in ``hubitat_tool.py``.

This tool is exposed on the voice surface so a spoken question like "what's the porch
temperature?" can actually be answered, and so lights and the thermostat can be controlled by
voice. It needs no terminal, which is the whole point: the documented CLI helper does.
"""

from __future__ import annotations

import json
import os

from .hubitat_tool import (
    HubitatError,
    all_devices,
    check_command_allowed,
    device_detail,
    display_name,
    find_device,
    hue_of_rgb,
    load_config,
    read_verified,
    summarise,
    _command_needs_verification,
)

_TOOLSET = "hubitat"

_SCHEMA = {
    "name": "hubitat",
    "description": (
        "Read and control Angela's Hubitat smart-home devices: lights, switches, plugs, fans, "
        "the thermostat, and sensors such as the Porch Thermometer, Hall Thermometer and the "
        "room thermometers. Use action='status' with a device name to read a sensor (for "
        "example the porch temperature), 'list' to discover exact device names, 'on'/'off' to "
        "switch something, 'set_level' for dimmers, and 'set_thermostat' for heating/cooling. "
        "Locks and doors are deliberately not operable here and the attempt will be refused. "
        "State changes are verified by reading the device back, and the reply includes a "
        "'verified' flag: report what was verified, never assume success."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "status", "on", "off", "set_level", "set_color",
                         "set_color_temperature", "set_effect", "effect_next",
                         "effect_previous", "set_thermostat"],
                "description": "What to do. 'status' reads a device without changing it.",
            },
            "device": {
                "type": "string",
                "description": ("Device name or numeric id, e.g. 'Porch Thermometer', "
                                "'Living Room Lights', 'Thermostat'."),
            },
            "level": {
                "type": "integer", "minimum": 0, "maximum": 100,
                "description": "Dim level percentage, for set_level.",
            },
            "kelvin": {
                "type": "integer", "minimum": 1000, "maximum": 7000,
                "description": "Colour temperature in Kelvin, for set_color_temperature.",
            },
            "hue": {
                "type": "integer", "minimum": 0, "maximum": 360,
                "description": "Hue in degrees, for set_color: red 0, yellow 60, green 120, "
                               "cyan 180, blue 240, magenta 300.",
            },
            "saturation": {
                "type": "integer", "minimum": 0, "maximum": 100,
                "description": "Saturation percentage, for set_color. 100 is vivid, 0 is white.",
            },
            "effect_number": {
                "type": "integer", "minimum": 0, "maximum": 500,
                "description": "Effect index, for set_effect. These Govee drivers take an effect "
                               "NUMBER, not a name; use effect_next to step through them.",
            },
            "mode": {
                "type": "string", "enum": ["cool", "heat", "auto", "off"],
                "description": "Thermostat mode, for set_thermostat.",
            },
            "temperature": {
                "type": "number",
                "description": "Target temperature for set_thermostat, in the hub's units.",
            },
        },
        "required": ["action"],
    },
}

_THERMOSTAT_MODE_COMMAND = {
    "cool": "setCoolingSetpoint",
    "heat": "setHeatingSetpoint",
    "auto": "setCoolingSetpoint",
}


def _need(args: dict, key: str) -> str:
    value = args.get(key)
    if value in (None, ""):
        raise HubitatError(f"action {args.get('action')!r} requires {key!r}")
    return str(value)


def _resolve(cfg: dict, args: dict) -> dict:
    query = _need(args, "device")
    device = find_device(all_devices(cfg), query)
    if device is None:
        raise HubitatError(f"no Hubitat device matches {query!r}")
    return device_detail(cfg, str(device["id"]))


def handle(args: dict) -> str:
    """Tool entry point. Always returns a JSON string, as tool handlers must."""
    action = str(args.get("action") or "").strip().lower()
    try:
        cfg = load_config()
    except HubitatError as exc:
        return json.dumps({"ok": False, "error": str(exc)})

    try:
        if action == "list":
            devices = all_devices(cfg)
            return json.dumps({
                "ok": True,
                "count": len(devices),
                "devices": [{"id": d.get("id"), "name": display_name(d)}
                            for d in sorted(devices, key=lambda x: display_name(x).lower())],
            })

        if action == "status":
            detail = _resolve(cfg, args)
            return json.dumps({"ok": True, **summarise(detail, detail=True)})

        if action in ("on", "off"):
            detail = _resolve(cfg, args)
            check_command_allowed(cfg, detail, action)
            device_id = str(detail["id"])
            _send(cfg, device_id, action)
            expect = _command_needs_verification(action)
            after = read_verified(cfg, device_id, expect)
            verified = expect is None or str(
                {a["name"]: a.get("currentValue") for a in (after.get("attributes") or [])
                 if isinstance(a, dict)}.get(expect[0], "")
            ).lower() == expect[1].lower()
            return json.dumps({"ok": True, "requested": action,
                               "device": display_name(after), "verified": verified,
                               **summarise(after)})

        if action == "set_level":
            level = int(args.get("level"))
            if not 0 <= level <= 100:
                raise HubitatError("level must be 0-100")
            detail = _resolve(cfg, args)
            check_command_allowed(cfg, detail, "setLevel")
            device_id = str(detail["id"])
            _send(cfg, device_id, "setLevel", [str(level)])
            after = read_verified(cfg, device_id, ("level", level))
            got = str({a["name"]: a.get("currentValue") for a in (after.get("attributes") or [])
                       if isinstance(a, dict)}.get("level", ""))
            return json.dumps({"ok": True, "requested": f"level {level}",
                               "device": display_name(after), "verified": got == str(level),
                               **summarise(after)})

        if action == "set_color":
            hue = int(args.get("hue"))
            saturation = int(args.get("saturation", 100))
            if not 0 <= hue <= 360:
                raise HubitatError("hue must be 0-360")
            if not 0 <= saturation <= 100:
                raise HubitatError("saturation must be 0-100")
            detail = _resolve(cfg, args)
            device_id = str(detail["id"])
            # Separate hue/saturation writes can overwrite one another with stale
            # companion attributes. Send hue alone when saturation already matches.
            # Do not suppress explicit requests based on cached RGB.
            # Physically verified via direct red and spoken blue requests.
            # See GOVEE-CONTROL.md for evidence, hue units, and saturation limits.
            before = {a["name"]: a.get("currentValue")
                      for a in (detail.get("attributes") or []) if isinstance(a, dict)}
            try:
                current_saturation = float(before.get("saturation"))
            except (TypeError, ValueError):
                current_saturation = None
            if current_saturation != saturation:
                raise HubitatError("Colour not sent: changing saturation together with hue "
                                   "needs an atomic colour command; separate writes can "
                                   "restore the previous colour. Current saturation is "
                                   f"{current_saturation}, requested {saturation}.")
            hue_percent = max(0, min(100, round(hue * 100 / 360)))
            check_command_allowed(cfg, detail, "setHue")
            _send(cfg, device_id, "setHue", [str(hue_percent)])
            if args.get("level") is not None:
                level = int(args["level"])
                if not 0 <= level <= 100:
                    raise HubitatError("level must be 0-100")
                check_command_allowed(cfg, detail, "setLevel")
                _send(cfg, device_id, "setLevel", [str(level)])
            # Read cached hub attributes without issuing another device command.
            # A matching value is not physical confirmation. Earlier claims that
            # refresh itself restored the old colour were not established.
            after = device_detail(cfg, device_id)
            state = {a["name"]: a.get("currentValue") for a in (after.get("attributes") or [])
                     if isinstance(a, dict)}
            observed_rgb = state.get("colorRGBNum") or state.get("RGB")
            observed_hue = hue_of_rgb(observed_rgb)
            close_enough = (observed_hue is not None
                            and abs((observed_hue - hue + 180) % 360 - 180) <= 20)
            colour = None
            try:
                colour = f"#{int(str(observed_rgb).strip()):06x}"
            except (TypeError, ValueError):
                colour = None
            return json.dumps({"ok": True,
                               "requested": f"hue {hue}, saturation {saturation}",
                               "device": display_name(after),
                               "observed_hue": None if observed_hue is None else round(observed_hue),
                               "colour": colour,
                               "verified": close_enough,
                               # Say where the "verified" came from. These drivers set their own
                               # attributes optimistically when commanded, so a matching read proves
                               # the command was accepted, NOT that the light changed. On
                               # 2026-09-22 a red change read back as #ff0000 verified while the
                               # light was still visibly green, which is exactly this trap.
                               "verified_source": "hub attributes, which this driver updates on "
                                                  "command rather than from the light",
                               "note": ("the hub reports the requested colour. These cloud lights "
                                        "can still lag or miss a push, so confirm visually if it "
                                        "matters" if close_enough else
                                        "command sent. This driver's readings lag the cloud, so "
                                        "confirm visually if it matters"),
                               **summarise(after)})

        if action in ("set_effect", "effect_next", "effect_previous"):
            detail = _resolve(cfg, args)
            device_id = str(detail["id"])
            if action == "set_effect":
                number = args.get("effect_number")
                if number is None:
                    raise HubitatError("set_effect needs an effect_number")
                command, payload = "setEffect", [str(int(number))]
            elif action == "effect_next":
                command, payload = "setNextEffect", []
            else:
                command, payload = "setPreviousEffect", []
            check_command_allowed(cfg, detail, command)
            _send(cfg, device_id, command, payload)
            # Read cached attributes only. This is not physical effect verification;
            # refresh-induced reversion has not been established.
            after = device_detail(cfg, device_id)
            return json.dumps({"ok": True, "requested": action, "device": display_name(after),
                               **summarise(after)})

        if action == "set_color_temperature":
            kelvin = int(args.get("kelvin"))
            detail = _resolve(cfg, args)
            check_command_allowed(cfg, detail, "setColorTemperature")
            device_id = str(detail["id"])
            _send(cfg, device_id, "setColorTemperature", [str(kelvin)])
            after = read_verified(cfg, device_id, ("colorTemperature", kelvin))
            return json.dumps({"ok": True, "requested": f"{kelvin}K",
                               "device": display_name(after), **summarise(after)})

        if action == "set_thermostat":
            mode = str(args.get("mode") or "").strip().lower()
            if mode not in _THERMOSTAT_MODE_COMMAND and mode != "off":
                raise HubitatError("mode must be one of cool, heat, auto, off")
            detail = _resolve(cfg, args)
            device_id = str(detail["id"])
            sent = []
            if mode == "off":
                check_command_allowed(cfg, detail, "setThermostatMode")
                _send(cfg, device_id, "setThermostatMode", ["off"])
                sent.append("mode off")
            else:
                temperature = args.get("temperature")
                if temperature is None:
                    raise HubitatError("set_thermostat needs a temperature")
                check_command_allowed(cfg, detail, "setThermostatMode")
                _send(cfg, device_id, "setThermostatMode", [mode])
                sent.append(f"mode {mode}")
                command = _THERMOSTAT_MODE_COMMAND[mode]
                check_command_allowed(cfg, detail, command)
                _send(cfg, device_id, command, [str(temperature)])
                sent.append(f"{command} {temperature}")
            after = read_verified(cfg, device_id)
            return json.dumps({"ok": True, "sent": sent, "device": display_name(after),
                               **summarise(after)})

        return json.dumps({"ok": False, "error": f"unknown action {action!r}"})

    except HubitatError as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - a tool must never raise into the agent
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _send(cfg: dict, device_id: str, command: str, command_args: list[str] | None = None) -> None:
    """Send a command to a device.

    The Maker API command URL is ``/devices/{id}/{command}[/{value}]``.

    An earlier version used ``/devices/{id}/command/{command}``, which the hub rejects with
    HTTP 500 (it parses that as a command literally named "command"). That bug was nasty because
    READS use ``/devices/{id}``, a perfectly valid path, so the tool appeared to work whenever it
    was only reading sensors: the porch thermometer answered correctly while every switch, dimmer
    and thermostat command silently failed. It also made a perfectly healthy outlet look like a
    dead device on a broken mesh.
    """
    from .hubitat_tool import _get_json
    import urllib.parse

    path = f"devices/{urllib.parse.quote(device_id)}/{urllib.parse.quote(command)}"
    for arg in command_args or []:
        path += f"/{urllib.parse.quote(str(arg))}"
    _get_json(cfg, path)


def register(ctx) -> None:
    """Register the hubitat tool with the plugin context."""
    ctx.register_tool(
        name="hubitat",
        toolset=_TOOLSET,
        schema=_SCHEMA,
        handler=lambda args, **kwargs: handle(args),
        check_fn=lambda: os.path.exists(
            os.path.expanduser(os.environ.get("HUBITAT_MAKER_API_JSON",
                                              "~/.openclaw/secrets/hubitat-maker-api.json"))),
        requires_env=[],
        description="Hubitat home control and sensor reads",
        emoji="🏠",
    )
