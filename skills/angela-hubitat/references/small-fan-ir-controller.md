# Small Fan IR Controller

## Overview

Custom Hubitat integration that controls Michael's small fan via IR commands sent to a Broadlink remote. Originally used a virtual fan driver + companion app; replaced 2026-06-15 with individual virtual switches for reliable Alexa voice control.

## Current Architecture (v2 — Virtual Switches, 2026-06-15)

Replaced the FanControl driver approach after confirming that the Echo Skill's ModeController has a persistent `commands: []` bug that cannot be fixed from the driver side.

- **Virtual Switches** (ids 726-731): Plain Virtual Switch devices (driver type 60). Each is momentary: turn on → Rule Machine sends IR code → turn off.
- **Rule Machine Rules**: One per switch. Trigger: switch turns on. Action: sendSavedCode to Broadlink, then turn switch off.
- **Alexa Routines**: One per voice command. Maps voice phrase to turning on the corresponding virtual switch.
- **Broadlink Remote** (id 692): Michael's Broadlink IR Remote, IP 192.168.0.196. Stores IR codes as state variables.

### Virtual Switches

| Device | ID | IR Code Name | Alexa Routine (example) |
|--------|-----|-------------|------------------------|
| Small Fan Power | 726 | Small Fan Power | "turn small fan on/off" |
| Small Fan Speed | 727 | Small Fan Speed | "cycle small fan speed" |
| Small Fan Max | 728 | Small Fan Max | "set small fan to max" |
| Small Fan Oscillate | 729 | Small Fan Oscillate | "oscillate small fan" |
| Small Fan Light | 730 | Small Fan Light | "toggle small fan light" |
| Small Fan Timer | 731 | Small Fan Timer | "set small fan timer" |

### Why Virtual Switches Instead of FanControl

The Echo Skill's ModeController consistently shows `commands: []` for custom FanControl drivers. This means Alexa cannot send speed commands through the fan speed interface. Tested extensively on 2026-06-15:
- Removing SwitchLevel did not fix ModeController commands
- Changing setSpeed typing did not fix it
- Modifying supportedFanSpeeds did not fix it
- Re-adding the device to Echo Skill did not fix it
- The bug appears to be in the Echo Skill's FanControl command mapping, not in driver code

Plain virtual switches use PowerController which has reliable command mapping. The trade-off is one device + one routine + one rule per function, but it works every time.

## Legacy Architecture (v1 — FanControl Driver, deprecated)

**Driver** (id 1738, v36): Virtual state management with FanControl + SwitchLevel capabilities. Code backed up to Obsidian vault at `20 Areas/Home Automation/Small Fan IR Controller/small-fan-driver-v36.groovy`.

**Companion App** (id 610/789, v13): Subscribed to device speed events, sent IR codes to Broadlink. Code backed up at `20 Areas/Home Automation/Small Fan IR Controller/small-fan-companion-v13.groovy`.

Both deleted 2026-06-15. App definition (610) could not be deleted via API — removed manually from Hubitat web UI if needed (harmless if left).

### Key Lessons from v1 Debugging

- ModeController `commands: []` is a persistent Echo Skill bug affecting ALL custom FanControl drivers
- `supportedFanSpeeds` must not include `"on"` (command alias, not a speed)
- `sendEvent()` should include `isStateChange: true, type: "digital", descriptionText`
- `setSpeed(String speed)` typed parameter matches BOND Fan v2 production driver
- SwitchLevel is required for PowerLevelController fallback (percentage routing via setLevel)
- "Respond immediately" must be enabled in Echo Skill (under "Show advanced device type options")
- Event handler ordering is NOT guaranteed in Hubitat — subscribe to only ONE event type
- `evt.getJsonValue()` does NOT exist — use `evt.value` and track state manually
- Apps use `/app/saveOrUpdateJson`, NOT `/driver/saveOrUpdateJson`

## Device Info

- **Broadlink Remote** (id 692): Michael's Broadlink IR Remote, IP 192.168.0.196
- **IR Code Names**: Small Fan Power, Small Fan Speed, Small Fan Max, Small Fan Oscillate, Small Fan Light, Small Fan Timer

## Button Functions

| Button | Action | IR Sequence |
|--------|--------|-------------|
| **On** | Power on (starts at low), then set last speed | Power → (1s) → Max → Speed presses |
| **Off** | Power off | Power |
| **Low** | Set speed 1 (Power if off, then Max + 1 Speed) | Power (if off) → Max → 1 Speed press |
| **Medium** | Set speed 4 (Power if off, then Max + 4 Speed) | Power (if off) → Max → 4 Speed presses |
| **High** | Set speed 8 (Power if off, then Max only) | Power (if off) → Max only |

## IR Protocol

The fan uses a **12-bit repeated code** with mark-distance encoding.

| Function    | Code (hex) | Repeats |
|-------------|-----------|---------|
| Power       | 0x2       | 8       |
| Max         | 0xA       | 4       |
| Speed       | 0x6       | 4       |
| Oscillate   | 0x4       | 6       |
| Light       | 0x8       | 6       |
| Timer       | 0xC       | 6       |

## Fan Speed Behavior

- **Power IR code** → toggles fan on/off. When turning ON, fan always starts at low speed automatically.
- **Max IR code** → sets fan to speed 8 (highest)
- **Speed IR code** → cycles through speeds: 8→1→2→3→4→5→6→7→8→1...

### Speed-to-Presses Mapping

| Speed    | Presses after Max | Result  |
|----------|-------------------|---------|
| low      | 1                 | speed 1 |
| medium   | 4                 | speed 4 |
| high     | 0                 | speed 8 |

**Pitfall:** Medium is 4 presses, NOT 3.

## Timing Requirements

- Speed presses at **1-second intervals** after Max, using recursive scheduling.
- **1-second delay between Power and Max** codes. If Power and Max arrive within ~100ms, the fan gets confused.

## Echo Skill Debug Log Patterns

When debug logging is enabled on the Echo Skill (app 12):

- `getAmazonCapabilities(DeviceName, true)` — Echo Skill querying device's Alexa interface mapping
- `amazonCapabilities: [...]` — The actual Alexa interfaces mapped. Check `"commands"` arrays — empty `[]` means the interface can't send commands
- `getReportState for device {id}` — Echo Skill reading device state for Alexa
- `buildEndpoint DeviceName` — Echo Skill building the Alexa endpoint for a device
- `removeDevices [N]` — Echo Skill removing stale device entries
- `Device DeviceName - Alexa categoryList: FAN,LIGHT,SWITCH` — Device categorization

### Diagnostic Signal

If `amazonCapabilities` shows `commands: []` for ModeController but lists speed modes in `supportedModes`, the Echo Skill cannot map speed commands. This is the ModeController bug. Use virtual switches instead.
