# Current Hubitat device and app catalog

Treat this catalog as changeable. Query Hubitat live before acting or claiming current state.

## HVAC

- Thermostat: device 1, Honeywell T6 Pro Z-Wave, label `Thermostat`. Modes: off, heat, cool, emergency heat; fan modes auto/on. Controls via `set-thermostat` in the helper script.

## Vehicles and energy

- Angela 2022 IONIQ 5: device 681, Hyundai Bluelink driver. Direct commands include Start, Stop, Lock, Unlock, Location, refresh.
  - **Reading telemetry (including mileage):** `status "Angela 2022 IONIQ 5"` returns the full attribute set, which includes `Odometer` (miles), `EVBattery` (%), `EVRange`, `EVBatteryCharging`, `EVBatteryPluggedIn`, `DoorLocks`, `Engine`, `Hood`, `Trunk`, `TirePressureWarning`, `locLatitude`/`locLongitude`, `VIN`, and `LastRefreshTime` in UTC. This is the supported way to pull current vehicle mileage; no other integration is needed.
  - **Refreshing before reporting:** `command "Angela 2022 IONIQ 5" refresh` triggers a Bluelink poll and takes roughly 15-30 seconds. **The helper frequently raises `TimeoutError` on this call even when the poll succeeds.** Never report the refresh as failed from the error alone: read back `LastRefreshTime` (it should advance to the current time) and confirm at least one attribute changed. Report the reading only after that check.
  - **Mileage-based tire rotation tracking is already automated.** The `no_agent` cron job `tire-rotation-mileage-check` runs `/home/angela/.hermes/scripts/tire-rotation-check.py` daily, reads the odometer through this device, and keeps one rotation task in Google Tasks `My Tasks`. Do not build a second mileage watcher; the baseline and rotation rules live in the Obsidian note `30 Reference/Vehicles/2022 Hyundai Ioniq 5.md`.
- **Angela's Car: device 682, virtual-switch trigger for the scripted remote-start automation. This is the ONLY correct way to remote start the car.** Use `on "Angela's Car"` / `off "Angela's Car"`. Do NOT send a Start command directly to device 681 — the virtual switch triggers a Hubitat automation that handles the Bluelink sequence. Each start runs about 10 minutes and cannot be extended while already running.
- Energy Meter: device 715, Aeon/Aeotec HEM v1 using custom driver 1734. Canonical project information is on the NAS under `AI Projects/Hubitat App Energy Meter/` in `memory/PROJECT_MEMORY.md`.
- Bluetti: device 716, desk power/solar/backup device.

## Broadlink and IR devices

- Michael's Broadlink IR Remote: device 692, Broadlink Remote driver 1587, IP 192.168.0.196.
- Small Fan virtual switches: devices 726-731 for Power, Speed, Max, Oscillate, Light, and Timer.
- Amplifier virtual switches: devices 732-736 for Power On, Power Off, Volume Up, Volume Down, and Aux.
- Samsung TV Power Toggle: device 737.
- IR Blast Companion: installed app 611. It maps momentary virtual switches to Broadlink saved-code names.

## Removed/legacy

- Small Fan device 724 and its custom fan controller were deleted on 2026-06-15. The individual virtual switches replaced the driver because Hubitat Echo Skill ModeController reporting was unreliable.
- Legacy source/history is in the Obsidian vault under `20 Areas/Home Automation/Small Fan IR Controller/`.

## Lighting and zones

- **Main room lights:** `Living Room Lights` (2, room `Entertainment Center`), `Dining Room` (3, Zooz Zen71,
  room `Dining Area`), `Kitchen Lights` (4, on/off only, room `Kitchen`).
- **Kitchen accessory lights:** `Stove Light` (546), `Counter Light` (548). Distinct from the main kitchen light.
- **Tealight:** `Tea Table` (25, room `Dining Area`).
- **Legacy:** `Dining Room (old)` (679, room `Dining Area`) sits alongside the current `Dining Room` (3). Both
  usually read the same; confirm whether it still controls anything before depending on it.
- **Entertainment center wall glow:** `Govee bulb one` (602), `Govee bulb two` (603), `Corner Light` (604), all
  `Govee v2 Color Lights Driver`, no Hubitat room. A per-bulb hue spread (for example 50 / 16 / 83 on the same
  effect at the same level) is intentional and produces a colorful wall; do not normalize it. Known looks: the
  colorful wall is RGB mode, and the daytime white look is CT mode at 2700 K, level 100. Their attributes are
  cached, so send `refresh` before reading them, and treat `effectName` / `colorName` as stored background
  settings rather than current state.
- **Michael's room:** `Fairy Lights` (601), `Govee 100` (605), plus `First Light` (13), `Second Light` (14),
  `Third Light` (15), `Desk Lamp` (687), `Closet` (174). Exclude these from living-room scenes.
- **House-wide ambiance:** `Air Purifier` (244). Off during movie-time style scenes.
- **Zoning gotcha:** living room and entertainment center are one physical room treated as two sides. Do not
  infer fixture membership from Hubitat room assignment; resolve presets to explicit device ids.

## Temperature sensors

- Porch Thermometer (35): outside sensor, Z-Wave. Cross-check against the OpenWeatherMap `Outside` device (550) when its reading looks off.
- Hall Thermometer (553), Angela Room Thermometer (717), Michael's Room Thermometer (718): inside climate. Normal day range roughly 64–78°F based on recorded min/max.
- Printing Closet Thermometer (721): **intentionally kept warm by design — all the time.** High readings (e.g. 85–100°F) are expected, not an anomaly. Do not flag the temperature as a concern; Angela confirmed this on 2026-09-10. Only battery or health failures warrant attention here.

## Verification

Use the helper script from the main skill for normal status/control. Use the internal Hubitat API only when the relevant development reference explicitly calls for it. Never assume a listed ID is still current without a live query.
