# IR Blast Companion Pattern

## Overview

When FanControl/ModeController doesn't work with Alexa (persistent `commands: []` bug), use individual virtual switches + a companion app. This is the most reliable approach for IR-controlled devices with multiple functions.

## Architecture

```
Voice Command → Alexa Routine → Virtual Switch ON → Companion App → Broadlink IR Code → Switch OFF
```

## Components

1. **Virtual Switches** (driver type 60): One per IR function. Plain on/off switches.
2. **IR Blast Companion App**: Subscribes to switch.on events, sends IR codes, turns switches off.
3. **Alexa Routines**: Map voice commands to turning on the appropriate switch.
4. **Broadlink IR Remote**: Stores named IR codes, receives `sendSavedCode` commands.

## Companion App Design

The IR Blast Companion app (id 611) is a generic, configurable app:

```groovy
// Key pattern:
def switchOnHandler(evt) {
    def dev = evt.device
    String codeName = getCodeName(dev)  // defaults to device label
    broadlinkDevice.sendSavedCode(codeName)
    dev.off()  // momentary: turn off after sending
}
```

Configuration: select Broadlink remote + select virtual switches. Code names default to device labels. Optional override field for custom mappings.

## Naming Convention

Name virtual switches to match IR code names on the Broadlink exactly:
- Device "Small Fan Power" → IR code "Small Fan Power"
- Device "Amplifier Volume Up" → IR code "Amplifier Volume Up"

This eliminates the need for code name overrides.

## When to Use This Pattern

- FanControl/ModeController shows `commands: []` in Echo Skill logs
- Device has multiple discrete functions (not just on/off/speed)
- IR blaster with named codes (Broadlink, RM4, etc.)
- Any device where Alexa's capability mapping fails

## Trade-offs

- One virtual switch + one Alexa routine per function
- Alexa routines must be created manually (no API)
- More devices to manage in Hubitat
- But: 100% reliable voice control, no state reporting issues
