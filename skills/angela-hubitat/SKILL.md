---
name: angela-hubitat
description: Hubitat Maker API home automation control for Angela; load before checking, controlling, or developing Hubitat devices.
version: 1.2.0
author: angela
platforms: [linux]
metadata:
  hermes:
    tags: [Hubitat, HomeAutomation, MakerAPI, SmartHome]
---

# Angela's Hubitat Home Automation

Load this skill before any Hubitat or home-automation operation. Do not guess endpoints, tokens, device IDs, or command names.

## Maker API command URL form (verified 2026-09-22)

The command path is `/devices/{id}/{command}[/{value}]`:

```
{hubitat_base_url}/apps/api/{app_id}/devices/{device_id}/{command}[/{value}]?access_token=...
```

`/devices/{id}/command/{command}` is **NOT valid**. The hub parses it as a command literally named
"command" and answers **HTTP 500 Server Error**.

Recognise this failure mode, because it hides: **reads use `/devices/{id}`, which is a valid path**, so
sensor and status reads keep working perfectly while every switch, dimmer and thermostat command fails.
The integration looks healthy and merely "cannot control anything", and a perfectly good device can be
misdiagnosed as dead, off the mesh, or needing re-pairing.

Diagnostic rule: before declaring an integration or a device healthy, exercise a **read and a write**.
A working read path can mask a completely broken write path.

## Normal control workflow

Use the helper instead of constructing Maker API calls manually:

```bash
# List devices (TSV)
python3 ~/.openclaw/workspace/scripts/hubitat-control.py list

# Read status (JSON)
python3 ~/.openclaw/workspace/scripts/hubitat-control.py status "Device Name"

# Ordinary controls
python3 ~/.openclaw/workspace/scripts/hubitat-control.py on "Device Name"
python3 ~/.openclaw/workspace/scripts/hubitat-control.py off "Device Name"
python3 ~/.openclaw/workspace/scripts/hubitat-control.py set-level "Device Name" 40
python3 ~/.openclaw/workspace/scripts/hubitat-control.py set-color-temperature "Device Name" 2700
python3 ~/.openclaw/workspace/scripts/hubitat-control.py set-thermostat "Device Name" cool 60
python3 ~/.openclaw/workspace/scripts/hubitat-control.py command "Device Name" "commandName" [args]
```

Configuration is stored in `~/.openclaw/secrets/hubitat-maker-api.json`; never print or reveal it. Workflow notes are in `~/.openclaw/workspace/memory/hubitat.md`.

## When Angela says "turn on" / "turn off" / "switch on" / "switch off" a device

This always means Hubitat. Do not question it, do not say you can't do it. Use the helper script to control the device. If the device name is ambiguous, list devices and confirm — but do not fail to recognize the request as a Hubitat operation.

## Thermostat operations

Canonical thermostat: `Thermostat` (Honeywell T6 Pro Z-Wave). Verify the live ID with `list` before acting, as always.

Use `set-thermostat` for mode + setpoint changes. It sends the mode command and the matching setpoint command (cool -> `setCoolingSetpoint`, heat/emergencyHeat -> `setHeatingSetpoint`, auto -> both), then `refresh` and reads back the state, printing JSON with a `verified` boolean. Exit code 0 = verified, 3 = sent but read-back does not match (report the mismatch, do not assume success). `--dry-run` prints the planned commands and current state without sending anything.

```
# One-liner, self-verifying:
python3 ~/.openclaw/workspace/scripts/hubitat-control.py set-thermostat "Thermostat" cool 60
```

Workflow notes:

- Do NOT read the helper script source to discover device commands. The helper is a generic Maker API passthrough with no device-specific shortcuts beyond the built-in subcommands; `status` already returns the device's full command list.
- When Angela gives a temperature without heat vs cool, use the active mode: check `thermostatMode` from `status` or `set-thermostat --dry-run` first, then set that mode with the requested temperature.
- Scheduled changes ("set the thermostat to 60 at 6 AM") are one-shot `cronjob_manage` jobs with an ISO timestamp (resolved in local time; confirm the returned `next_run_at` offset is local, e.g. `-06:00` for MDT), with this skill attached and a prompt that runs the single `set-thermostat` call and reports the `verified` field and actual values. If `verified` is false, report the mismatch rather than retrying blindly.

## Capturing lighting presets / named scenes

Angela names lighting looks informally ("movie time") rather than creating Hubitat groups. Maker API cannot
export groups, scenes, or rooms-to-fixtures, so a preset is a hand-captured attribute set. Canonical record:
Obsidian `20 Areas/Home Automation/Hubitat Lighting Presets.md`.

Procedure:

1. Resolve the preset to an **explicit device id list and confirm it with Angela**. Hubitat room names do not
   match her mental zoning: `Living Room Lights` (2) and the `Workshop` outlet (5) are both assigned to the
   Hubitat room `Entertainment Center`, living room and entertainment center are the same physical room, and
   the Govee lights carry no room at all. A room-based query returns the wrong set.
2. Read every device in the set with `status`. Capture `switch` plus `level` for dimmers/switches/outlets, and
   `colorMode`, `effectName`, `level`, `hue`, `saturation`, `colorTemperature` for color lights.
3. **Capture what must be OFF as well as what must be ON.** In these scenes the off set is the point: leaving
   the kitchen accessory lights, tealight, dining room, workshop outlet, air purifier, or living room lights on
   ruins the look. A replay must set them off explicitly.
4. Replay order for an effect light: `on`, then `set-level`, then `setEffect`. A level change can cancel an
   active light effect, so the effect must be applied last.
5. Re-read the whole set before replaying and report drift. Maker API returns last-known attributes.
6. Only devices exposed in the Maker API can be captured. The IR virtual switches (small fan 726-731,
   amplifier 732-736, Samsung TV 737) are **not exposed**, so amp/TV state cannot go in a preset.

Do not silently "correct" hue values that look inconsistent across a group. A deliberate spread of hues across
sibling color lights is the scene.

## Govee lights: cached attributes and sticky effects

These two quirks have already caused one wrong capture. Check them before reading, reporting, or recording any
Govee state.

- **Attributes are cached and can disagree with the physical light.** Changes outside Hubitat may remain stale until polling or refresh; do not assume every driver polls or that a matching readback proves delivery. For standalone state capture, refresh and allow the cloud response to arrive, but during command-sequence diagnosis avoid adding refreshes or extra writes that confound the test. Prefer Angela's visual confirmation when available. Refresh itself has not been established as a colour-reversion cause.
- **`effectName` and `colorName` are stored settings, not current state.** A bulb can report
  `effectName=Aurora` while actually outputting white in CT mode. Angela's rule: the effect setting stays in the
  background regardless of the current status. Judge the current look from `colorMode`, `colorTemperature`, and
  the RGB values. Do not clear, change, or re-apply an effect when replaying a preset unless the effect is
  genuinely the look.
- A white-light Govee scene is `colorMode=CT` with a `colorTemperature` (2700 K for the wall glow) and a level.
  A colorful scene is `colorMode=RGB` with per-bulb hues.

## Colour-command sequencing

- For Govee colour debugging, isolate `setHue` from `setSaturation`. Angela visually confirmed that hue-only blue stayed blue, whereas the voice plugin's immediate hue-then-saturation sequence flashed back to the previous colour. Upstream `Mavrrick.Govee_Cloud_RGB.groovy` implements `cloudSetSaturation` using `device.currentValue("hue")`, supporting a stale-companion-attribute race; the installed library implementation has not yet been retrieved.
- Angela confirmed the patched direct-plugin red request stayed red and the subsequent full spoken satellite blue request stayed blue. Use this as the end-to-end evidence for the Fairy Lights, not as proof for all Govee models. The helper docstring and `~/.hermes/plugins/hubitat/GOVEE-CONTROL.md` document the procedure and limits. Driver hue uses 0-100 (red 0, green 33, blue 67); plugin input uses degrees.
- The Hermes plugin now sends hue alone when requested saturation equals the cached saturation. It refuses combined saturation changes rather than issuing the unsafe pair. An atomic hue+saturation path remains to be implemented and verified. Regression tests: `~/.hermes/plugins/hubitat/test_color_regression.py`.
- Never suppress an explicit colour command solely because cached RGB matches. Hub attributes previously said red while Angela saw green. Matching attributes do not verify physical colour.
- Previous session claims that refresh writes the old colour, the poll causes reversion, or cloud congestion explains these failures were not established. Do not treat those claims in historical notes as proven causes.

- **A device state that disagrees with what was asked is not proof the command path failed, or that a
  second command went out.** Someone else in the house can change a light by hand or from a phone app:
  on 2026-09-22 the Govee fairy lights were changed manually as a joke, which looked exactly like the
  voice assistant acting on a request it was never given. Before investigating a suspected stray action,
  settle it from the assistant's own tool-call record, which is stronger evidence than any attribute
  readback or recollection:

  ```
  sqlite3 ~/.hermes/state.db "SELECT role, substr(COALESCE(tool_calls, content), 1, 300) FROM messages
    WHERE session_id LIKE 'voice-wyoming1%' ORDER BY rowid DESC LIMIT 20;"
  ```

  Every command the assistant issued is listed there with its verified result. If a device command is
  absent from that record, the assistant did not send it, whatever the light did.

- **Confirmation from the person beats any readback.** Ask what she sees; the driver can be stale and a
  human's eyes cannot. This has been the fastest way to resolve a disputed light state every time.

## Safety

- Lock-code operations are forbidden. Never add, remove, change, fetch, or reveal lock codes.
- Ask before operating home locks, doors, utility switches, reboot controls, or another risky device.
- The helper's risky-device flag is `--allow-risky`; use it only after explicit confirmation.
- Do not auto-fix drivers, apps, Alexa integration, or device configuration. Present findings and proposed changes first.
- Ordinary reads, lights, harmless plugs, fans, scenes, thermostat settings, and authorized Hyundai actions are normal safe operations.

## Verification

Verify every state-changing action before reporting success:

- Switches/dimmers: read status and confirm the relevant attribute.
- Thermostats: refresh, then read mode/setpoint because cached values may lag.
- Batches: verify every requested device individually.
- Hyundai/IONIQ: Hubitat state may be stale. Prefer the main Hyundai device for direct commands and use confirmation email when available for remote-start verification.

If Angela gives a thermostat temperature without specifying heat versus cool, use the active thermostat mode, then verify the resulting mode and setpoint.

## Development routing

For driver/app development or debugging, read `references/driver-and-app-development.md` before changing code. Core rules:

- Drivers manage virtual state; companion apps send physical IR/RF commands.
- Do not send the physical command from both layers.
- In a driver, resolve cross-device IDs with `getDeviceAt()`; `capability.*` selectors belong in apps.
- Event-handler ordering is not guaranteed.
- For Alexa-controlled virtual devices, use `isStateChange: true`; complex IR controls are most reliable as momentary virtual switches plus a companion app.

## Reference map

- `references/current-device-catalog.md`: live-device routing hints and known IDs; verify live before use.
- `references/driver-and-app-development.md`: driver/app architecture, internal APIs, deployment, and release workflow.
- `references/alexa-capabilities.md`: Alexa capability mapping and Echo Skill behavior.
- `references/broadlink-ir-integration.md`: Broadlink architecture, backup, and mappings.
- `references/broadlink-ir-protocol.md`: IR protocol details.
- `references/ir-blast-companion-pattern.md`: momentary-switch companion-app pattern.
- `references/small-fan-ir-controller.md`: small-fan implementation and debugging history.
- `references/small-fan-session-log-2026-06-15.md`: dated legacy session record.
- `references/samsung-tv-ir-codes.md`: Samsung TV protocol and tested codes.

## Final checks

Before reporting:

1. Confirm the requested device, not a similarly named device.
2. Confirm authorization for risky state changes.
3. Execute through the helper unless a development reference requires the internal API.
4. Read back state or use the strongest available external confirmation.
5. Report the verified state in plain language; show raw output only on failure or request.
6. On the **voice satellite surface**, keep it to one line naming the action, for example "I turned
   off the porch light", and never narrate the verification itself. The check still happens; a
   spoken reply arriving at all already implies it worked. Angela asked for this explicitly, and
   the voice prompt in `~/hermes-voice-bridge/bridge/config.py` (`VOICE_INSTRUCTIONS`) says the same,
   so keep the two in step if either changes.
