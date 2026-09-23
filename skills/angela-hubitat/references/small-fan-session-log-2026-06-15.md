# Small Fan IR Controller - Session Log 2026-06-15

## What was attempted

Custom FanControl driver (v33→v36) + companion app for Alexa-controlled IR fan via Broadlink.

### Iterations
- **v33**: Original. FanControl + SwitchLevel. ModeController `commands: []`. Speed commands failed.
- **v34**: Removed "on" from supportedFanSpeeds, added type/descriptionText. No improvement.
- **v35**: Removed SwitchLevel (thinking it conflicted). ModeController still `commands: []`, AND PowerLevelController disappeared. Voice commands completely broken.
- **v36**: Re-added SwitchLevel. PowerLevelController restored (percentage-based speed). Combined with "respond immediately" gives partial voice control.

### Root cause
Hubitat Echo Skill has a persistent bug: ModeController `commands: []` for ALL FanControl drivers. The Echo Skill discovers speed modes in `supportedModes` but never maps `setSpeed` to a ModeController command. This is NOT fixable from the driver side.

### What ultimately worked
Bypass FanControl entirely. Virtual switch per IR code + companion app + Alexa routines.

## Current architecture (as of 2026-06-15)

### Virtual switches (plain Virtual Switch, driver type 60)
| Device | ID | IR Code |
|--------|-----|---------|
| Small Fan Power | 726 | Small Fan Power |
| Small Fan Speed | 727 | Small Fan Speed |
| Small Fan Max | 728 | Small Fan Max |
| Small Fan Oscillate | 729 | Small Fan Oscillate |
| Small Fan Light | 730 | Small Fan Light |
| Small Fan Timer | 731 | Small Fan Timer |

### Amplifier virtual switches
| Device | ID | IR Code |
|--------|-----|---------|
| Amplifier Power On | 732 | Amplifier Power On |
| Amplifier Power Off | 733 | Amplifier Power Off |
| Amplifier Volume Up | 734 | Amplifier Volume Up |
| Amplifier Volume Down | 735 | Amplifier Volume Down |
| Amplifier Aux | 736 | Amplifier Aux |

### IR Blast Companion app (id 611)
Generic companion app. Subscribes to switch.on events, calls `sendSavedCode` on Broadlink (id 692), turns switch off. IR code names default to device labels. Source backed up to this folder.

### Samsung TV virtual switch
| Device | ID | IR Code |
|--------|-----|---------|
| Samsung TV Power Toggle | 737 | Samsung TV Power Toggle |

Samsung TV uses NEC protocol with address 0x07, power toggle = function 0x02. No discrete on/off codes exist. See `references/samsung-tv-ir-codes.md` for full protocol details and tested codes.

### IR codes on Broadlink (id 692)
- Small Fan Power, Small Fan Speed, Small Fan Max, Small Fan Oscillate, Small Fan Light, Small Fan Timer
- Amplifier Power On, Amplifier Power Off, Amplifier Volume Up, Amplifier Volume Down, Amplifier Aux
- Samsung TV Power Toggle

### Alexa side
Voice commands → Alexa routines → turn on virtual switch. Must be created manually in Alexa app (no API for routine creation).

## Old code backups (for reference only, not in use)
- `small-fan-driver-v36.groovy` — FanControl driver, v36
- `small-fan-companion-v13.groovy` — Old companion app with speed-event subscription pattern

## Key lessons
1. FanControl/ModeController is broken in the Echo Skill for custom drivers. Don't fight it.
2. Virtual switches + companion app is more reliable and more maintainable.
3. `capability.*` works in apps but NOT in drivers. Use `getDeviceAt()` in drivers.
4. Event handler ordering is NOT guaranteed. Subscribe to ONE event type only.
5. Rule Machine has no API. Companion apps are the automatable alternative.
6. Alexa routines have no API. Must be created manually.
8. Samsung NEC protocol uses NON-complemented address bytes (0x0707, not 0x07F8). Standard NEC complements the address — Samsung doesn't. Function bytes ARE complemented normally.
9. GitHub gist IR codes are in base64 format; Hubitat Broadlink driver uses raw hex format. They're incompatible — can't mix and match.
10. When generating discrete IR codes from a toggle: test each generated code individually. Function 0x03 mapped to the timer button on Angela's Samsung TV, not power off.
11. Deleting app definitions (Apps Code) requires the web UI. API can only delete installed instances.
12. Samsung TV WebSocket API: port 8001 `samsung.remote.control` returns `unauthorized` (needs pairing token); port 8002 `ms.remote.control` connects but rejects method (TV doesn't support Tizen v2 API). IR is the only reliable control path for Angela's TV.
13. Samsung NEC protocol address 0x07: function 0x02 = toggle, function 0x03 = timer (NOT power off). Don't assume function codes map to expected buttons — always test.
