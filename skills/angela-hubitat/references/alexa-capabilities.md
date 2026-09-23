# Alexa Capabilities through Hubitat Amazon Echo Skill

## Supported Device Types

| Hubitat Capability | Alexa Device Type | Voice Commands |
|---|---|---|
| Switch | On/Off Switch | "Alexa, turn on/off [device]" |
| Switch Level (Dimmer) | Dimmer | "Alexa, set [device] to 50%", "dim", "brighten" |
| Fan Control | Fan | "Alexa, set [device] to low/medium/high" |
| Lock | Smart Lock | "Alexa, lock/unlock [device]" |
| Garage Door Control | Garage Door | "Alexa, open/close [garage door]" |
| Shade Control | Window Covering | "Alexa, open/close [shade]", "set to 50%" |
| Thermostat | Thermostat | "Alexa, set to 72 degrees", "set to heat/cool/auto" |
| Contact Sensor | Contact Sensor | "Alexa, is [sensor] open/closed?" |
| Motion Sensor | Motion Sensor | "Alexa, is [sensor] detecting motion?" |
| Temperature Sensor | Temperature Sensor | "Alexa, what's the temperature on [sensor]?" |
| Humidity Sensor | Humidity Sensor | "Alexa, what's the humidity on [sensor]?" |

## NOT Supported by Alexa Skill

- Buttons (virtual or physical)
- Input devices
- AudioVolume / StepSpeaker (no native Alexa interface)
- Fan direction, oscillation, timer
## Color (RGB) control — corrected 2026-09-14

Full RGB color DOES work through the Echo Skill for compatible RGBW bulbs (voice: "Set the [device] to blue", "make it warmer/cooler"). Community reports confirm Sengled Element Color Plus owners controlling color via the skill. A missing color interface means a stale Alexa capability profile, not a driver limitation. (Supersedes the old note claiming full RGB color control is unsupported by the skill.)

- Driver swap does NOT fix color support: user-verified that the built-in Sengled driver and the Generic Zigbee RGBW driver behave identically in Alexa.
- Fix: remove device from Alexa app -> de-select in Echo Skill app -> re-select -> rediscover devices.
- Keep the driver's "Color Pre-staging" setting OFF; it worsens CT<->RGB desync.
- Known flakiness (community-reported, current): CT<->RGB voice transitions can time out with "device unresponsive" or show stale state in the Alexa app while the bulb actually changed. A group device sometimes behaves better than a single bulb.
- Fallback if a color interface still doesn't appear after clean re-discovery: Hue Integration emulation.

## Multi-Capability Devices

When a device has multiple capabilities (e.g., switch + temperature), Alexa may show it under the most recent capability type discovered. Workaround: re-discover devices after changing drivers.

## Fan Control Details

Alexa Fan Control supports these speed levels:
- off
- low
- medium
- high

The built-in Hubitat Virtual Fan driver does NOT register with Alexa. Use the **Enhanced Virtual Fan Controller** community driver by Joel Wetzel instead:
- GitHub: https://github.com/joelwetzel/Hubitat-Miscellaneous-Drivers
- File: `EnhancedVirtualFanController.groovy`
- Capabilities: Switch, Switch Level, Fan Control
- Speed levels: off, low, medium, high (medium-low, medium-high, auto are restricted)

### Custom Fan Drivers: Required Capabilities and Patterns

When building a custom driver that should appear as a fan in Alexa, declare these capabilities:

```groovy
capability "Switch"
capability "FanControl"
capability "SwitchLevel"    // REQUIRED — creates PowerLevelController for voice speed control
```

A custom `attribute "speed", "string"` is NOT recognized by the Echo Skill — Alexa will only see the Switch capability.

**CRITICAL: ModeController `commands: []` is a persistent Echo Skill bug.** The Echo Skill consistently fails to map `setSpeed` to ModeController commands for custom FanControl drivers. This is NOT fixable by removing SwitchLevel — tested 2026-06-15, removing SwitchLevel did not populate ModeController commands and broke the PowerLevelController fallback. The working solution is to keep SwitchLevel (so PowerLevelController exists) and enable "respond immediately" in the Echo Skill. Voice speed commands route through PowerLevelController as percentages: Alexa sends `setLevel(50)` for "medium", the driver's `setLevel()` converts to speed. With "respond immediately," Alexa responds instantly.

**App control vs voice routing:** Alexa app manual controls send commands directly to the device (always works). Voice commands go through Alexa's NLP → device type matching → capability routing. If voice commands don't work but app controls do, Alexa's cached device type may be wrong. Fix: remove device from Alexa app entirely and re-discover.

#### setSpeed should be typed String

The BOND Fan v2 driver (production, works with Alexa) uses `void setSpeed(String speed)`:

```groovy
void setSpeed(String speed) {    // CORRECT — matches BOND Fan v2
void setSpeed(speed) {           // Also works but untyped
```

#### sendEvent pattern for virtual fan drivers

All `sendEvent()` calls should include `isStateChange: true`, `type: "digital"`, and `descriptionText`:

```groovy
sendEvent(name: "switch", value: switchVal, isStateChange: true, type: "digital",
          descriptionText: "${device.displayName} switch is ${switchVal}")
sendEvent(name: "speed",  value: normalized, isStateChange: true, type: "digital",
          descriptionText: "${device.displayName} speed is ${normalized}")
sendEvent(name: "level",  value: level, isStateChange: true, type: "digital",
          descriptionText: "${device.displayName} level is ${level}")
```

The `type: "digital"` parameter helps the Echo Skill distinguish virtual events from physical state events. Production drivers (BOND Fan v2, Modern Forms) include these on every event.

#### Supported fan speeds attribute

Declare `supportedFanSpeeds` so Alexa knows valid speed values. This MUST be explicitly declared in metadata AND sent via `sendEvent()` — stale values from a previous driver version are not sufficient. The Echo Skill uses this to build the ModeController interface. Without it, the Echo Skill maps `"commands": []` for ModeController, meaning Alexa can't send speed changes even though it lists the speed modes.

```groovy
import groovy.json.JsonOutput

attribute "supportedFanSpeeds", "JSON"

// In initialize() or updated():
private void updateSupportedFanSpeeds() {
    List speeds = ["off", "low", "medium", "high"]
    sendEvent(name: "supportedFanSpeeds", value: JsonOutput.toJson(speeds), isStateChange: true)
}
```

**Pitfall — do NOT include `"on"` in `supportedFanSpeeds`.** `"on"` is a command alias (the driver maps it to the last speed), not an actual fan speed value. Including it may confuse the Echo Skill's ModeController command mapping, potentially resulting in `commands: []`. Use only `["off", "low", "medium", "high"]`.

Real-world reference: BOND Fan v2 driver (production, actively maintained) explicitly sends this via `JsonOutput.toJson()`.

#### After updating a driver

1. Verify capabilities with `device/fullJson/{id}` — should include FanControl, Switch (NOT SwitchLevel)
2. Remove device from Echo Skill → save → re-add → save (forces profile rebuild)
3. Re-discover devices in the Alexa app (Devices -> + -> Add Device -> Other -> Discover Devices)
4. Enable "respond immediately" for the fan device in Echo Skill advanced settings

## Alexa Hunches

Amazon enables "Alexa Hunches" by default, which can cause devices to turn on/off unexpectedly. Disable it:
- Amazon support: https://www.amazon.com/gp/help/customer/display.html?nodeId=G63YSH7WU32D9GW9
- Also check Routines page in Alexa app to remove any automations Hunches created

## Per-Device Customization

In the Amazon Echo Skill app on Hubitat, you can:
- Remove specific capabilities (e.g., remove dimmer from a dimmer to only allow on/off)
- Change response behavior (wait for device vs respond immediately)
- Move devices to an "Ignored" list to hide from Alexa

Enable "Show advanced device type options" to see per-device capability controls.

**For fan devices, ALWAYS enable "respond immediately".** This makes Alexa respond with "OK" immediately without waiting for state confirmation. Without it, on/off has ~30-45s delay and speed changes show "unresponsive" because the Echo Skill never calls `getReportState` for fan devices.

## Troubleshooting

### Alexa says "device doesn't support that"
- Check if the capability is actually exposed by the driver
- Verify with `device/fullJson/{id}` that capabilities include what you expect

### Devices don't appear in Alexa
- Alexa app → Devices → + → Add Device → Other → Discover Devices
- Check the device is selected in the Echo Skill app: `http://<hub>/installedapp/configure/12`

### Device appears under wrong type
- Delete from Alexa, de-select in Hubitat Echo Skill app, re-select, and rediscover

### ModeController has empty commands array

If Echo Skill debug logs show `amazonCapabilities` with `"commands": []` for ModeController (but the speed modes are listed in `supportedModes`), there are two common causes:

1. **`supportedFanSpeeds` not sent properly** — must be declared in metadata AND sent via `sendEvent()` with `JsonOutput.toJson()`. Use only `["off", "low", "medium", "high"]` — do NOT include `"on"`.

2. **SwitchLevel capability is present** — this causes the Echo Skill to create `PowerLevelController` which intercepts speed commands as percentages, leaving ModeController with empty commands. **Remove `capability "SwitchLevel"` from the driver.** Confirmed by debugging 2026-06-15.

After fixing, remove the device from Echo Skill → save → re-add → save → rediscover in Alexa app.

### Alexa shows "unresponsive"
This means Alexa can send commands but isn't getting proper state feedback from the device driver. Common causes:

1. **Device not in Echo Skill**: Navigate to `http://<hub>/installedapp/configure/12` and verify the device is in the selected devices list
2. **Cached old capability profile**: After changing driver capabilities, Alexa may still use the old profile. Fix: remove device from Echo Skill → save → re-add → save → rediscover in Alexa app
3. **`sendEvent()` filtered by Hubitat**: By default, Hubitat suppresses events where the value hasn't changed. Fix: add `isStateChange: true` to `sendEvent()` calls
4. **Echo Skill waiting for state report**: Alexa waits for the device to report a new state before responding. Fix: enable "respond immediately" in the Echo Skill app
5. **SwitchLevel causing ModeController `commands: []`**: Remove SwitchLevel from the driver — see ModeController section above

**Key distinction**: The "unresponsive" issue is between Alexa and the device DRIVER only. If you have a companion app that sends IR/RF codes, it is irrelevant to this issue. Don't modify the companion app when debugging Alexa problems.

### Echo Skill Debug Logging

Enable debug logging on the Echo Skill app (app 12) to see how Alexa maps devices. Key log patterns:

- `getAmazonCapabilities(DeviceName, true)` — Echo Skill querying the device's Alexa interface mapping
- `amazonCapabilities: [...]` — The actual Alexa interfaces mapped. Check the `"commands"` arrays — empty `[]` means the interface can't send commands to the device
- `getReportState for device {id}` — Echo Skill reading device state for Alexa
- `deviceHandler: {attribute} {value}` — Echo Skill successfully reading a device attribute value
- `removeDevices [N]` — Echo Skill removing stale device entries
- `buildEndpoint DeviceName` — Echo Skill rebuilding the device endpoint after re-add

**Diagnostic signal:** If `amazonCapabilities` shows ModeController with `"commands": []` but lists speed modes in `supportedModes`, check for SwitchLevel capability and `supportedFanSpeeds` issues.

**Known bug:** The Echo Skill never calls `getReportState` for fan devices after speed commands (confirmed in debug logs — 30+ other devices get polled, fan devices do not). This is why "respond immediately" must be enabled for all fan devices.

### Thermostats may not report current temperature back to Alexa
- Known limitation -- Alexa says "doesn't support that" when asked for current temp
