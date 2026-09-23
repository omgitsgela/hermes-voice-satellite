# Govee colour control: sequencing and verification

## Confirmed working behaviour

On 2026-09-22 Angela confirmed the Fairy Lights (device 601, Govee v2 Color
Lights 3 Driver) changed to blue then reverted under the old voice plugin.
A single `setHue 67` changed them to blue and stayed. After the plugin fix,
a direct plugin red request stayed red, and a spoken satellite blue request
changed them to blue and stayed. These are physical confirmations, not just
Hubitat attribute matches. Other Govee models have not been individually tested.

## Required sequence

- Send only `setHue` when requested saturation equals the hub's reported saturation.
- Do not append redundant `setSaturation`. The previous immediate two-write sequence
  produced the flash-back. Upstream `cloudSetSaturation` builds a complete colour
  using `device.currentValue("hue")`, supporting the stale-hue race explanation.
  The installed included library source has not been retrieved to establish exact parity.
- Driver hue is 0-100; the plugin accepts degrees and converts with
  `round(hue * 100 / 360)`. Red: 0; green: 33; blue: 67 at the driver.
- Never skip an explicit request because cached RGB matches the target. The hub
  reported red while Angela physically saw green during diagnosis.
- The generic CLI is a passthrough, not a sequence guard. Callers must follow these rules.

## Current limits

`set_color` refuses before writing if requested saturation differs from, or cannot
be compared with, the cached saturation. Atomic hue+saturation support remains
unimplemented. Do not claim pastel/white saturation transitions are fixed.
The colour handler is shared across devices, not Govee-specific; its saturation
restriction therefore applies to other devices using this action too.
Optional level changes exist but were not part of the successful colour-only tests.
The `verified` result currently means a matching hub hue readback, not physical
confirmation of hue, saturation, brightness, or persistence. Do not overstate it.

## Corrections to earlier explanations

Refresh-induced reversion, polling-induced reversion, and cloud congestion were
not established as causes. Removing refreshes did not fix the voice sequence.
Do not reproduce those claims as facts or add sleeps/retries without evidence.

## Code and tests

- Plugin: `~/.hermes/plugins/hubitat/__init__.py`, `handle`, `set_color` branch.
- Helper: `~/.openclaw/workspace/scripts/hubitat-control.py`.
- Offline tests: `python3 ~/.hermes/plugins/hubitat/test_color_regression.py`.
  They check hue-only dispatch and that cached target RGB does not suppress a request.
- After a behavioural code change, reload the gateway and verify a new running PID
  before asking for a spoken test. Documentation-only edits need no restart.
- Upstream reference:
  https://github.com/Mavrrick/Hubitat-by-Mavrrick/blob/main/Govee/v2/Mavrrick.Govee_Cloud_RGB.groovy
