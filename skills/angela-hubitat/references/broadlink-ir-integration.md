# Broadlink IR Integration on Hubitat

## Architecture

The Broadlink integration has three layers:

### 1. Broadlink Remote Driver (Virtual Device)
- Type: "Broadlink Remote" (community driver by @tomw)
- Each physical Broadlink RM device gets a virtual device in Hubitat
- Stores IR/RF codes as state variables under the `codes` key
- Commands: `learnIR`, `learnRF`, `saveLearnedCode`, `sendSavedCode`, `sendCodeData`, `push`
- GitHub: https://github.com/tomwpublic/hubitat_broadlink

### 2. Broadlink System Manager (App)
- Central code management app for multiple Broadlink Remote devices
- Can save codes FROM virtual devices into app storage
- Can sync codes TO virtual devices from app storage
- Can import Pronto codes, LIRC files, and old codeStore strings

### 3. Rule Machine Rules + Virtual Switches (per-device controls)
- Each IR-controlled function gets a Virtual Switch
- A Rule Machine rule triggers on switch → on, sends IR code, then turns switch off
- This makes IR buttons available in dashboards and Rule Machine

## IR Code Backup

### State Variable Access
Maker API does NOT expose state variables. Use internal web API:
```
GET {base_url}/device/fullJson/{device_id}
```
- IR codes: `data["deviceState"]["codes"]` (dict of name → hex string)
- Device attributes: `data["device"]["currentStates"]`
- Apps using device: `data["appsUsingForDialog"]`

### Backup Locations
- **Obsidian vault**: `20 Areas/Home Automation/Broadlink IR Remote Backup.md`
- **Rolling JSON**: `~/.openclaw/workspace/broadlink_ir_rolling_backup.json`
  - Keeps last 10 backups
  - Each entry has timestamp, codes dict, code count

### Backup Script Pattern
```python
import json, urllib.request
from datetime import datetime

with open('/home/angela/.openclaw/secrets/hubitat-maker-api.json') as f:
    config = json.load(f)

url = f"{config['hubitat_base_url']}/device/fullJson/692"
data = json.loads(urllib.request.urlopen(url, timeout=15).read().decode())
codes = data.get("deviceState", {}).get("codes", {})
```

## Known Devices

### Michael's Broadlink IR Remote (id 692) - Michael's Room

Current codes (as of 2026-06-14):
- **Amplifier**: Power On, Power Off, Volume Up, Volume Down, Aux
- **Small Fan**: Power, Speed, Max, Oscillate, Light, Timer

Previous code sets are preserved in the rolling backup JSON and Obsidian vault note.

## Creating New IR Buttons

1. Create Virtual Switch (Devices → Add Virtual Device → type: Virtual Switch)
2. Create Rule Machine rule:
   - Trigger: switch changed → on
   - Action: sendSavedCode to Broadlink Remote with code name
   - Action: turn switch off (reset for next trigger)
3. Add to dashboard

## Alexa Integration for IR Devices

For voice-controlling IR devices through Alexa, see `references/alexa-capabilities.md`.

Key constraint: Alexa Fan Control only supports on/off + speed levels. Use the **Enhanced Virtual Fan Controller** community driver (not the built-in Virtual Fan) for Alexa-compatible fan devices. Extra functions (oscillate, light, timer) must be separate Virtual Switches.

## IR Protocol Details

The Broadlink hex format encodes timing values at 32.84us resolution with escape coding for values > 255. The `00 01` bytes in hex are NOT separators -- they encode a single 8.4ms gap. See `references/broadlink-ir-protocol.md` for the full encoding format, python-broadlink source analysis, and the Small Fan's 12-bit repeated code protocol with function code table.

## Pitfalls

- Maker API `/devices/{id}` returns attributes but NOT state variables
- State variables require `/device/fullJson/{id}` (internal web API, no auth token needed for local access)
- `learnedCode` attribute is ephemeral (only holds the last learned code)
- Rule Machine rules show as "appsUsingForDialog" in the fullJson response
- Virtual Switches are type "Virtual Switch", not "Broadlink Remote"
- Built-in Hubitat Virtual Fan driver does NOT register with Alexa. Must use Enhanced Virtual Fan Controller.
- Alexa's Fan Control capability has no native support for oscillate, light, timer, or fan direction
