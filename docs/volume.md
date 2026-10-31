# Volume control

**Where this sits:** stage 8 play of the voice pipeline. The end-to-end map, and every other stage, is in `docs/architecture.md`.

Issue #10. Voice control of the satellite's output level, with one rule that governs the
whole design.

## The rule

**No path may take the device to silence.**

A voice device at zero volume is unreachable. You cannot talk it back up, because you
cannot hear it confirm anything: no reply, no tone, no alarm. So there is a floor, it is
above zero, and every level that reaches the device passes through it.

That is enforced in three independent places, deliberately:

| Where | What it does |
|---|---|
| `bridge/volume.py` `clamp()` | the Gateway's choke point; raises anything below the floor |
| `pi/volume_control.py` `AlsaVolume.clamp()` | the device's own choke point, so a bug or a bad desired value on the host still cannot produce a deaf device |
| `scripts/calibrate-volume.py` | refuses to record a floor of 0, and validates on read |

Each of the three is pinned by its own tests. Three is not redundancy for its own sake: the
gateway and the device are different machines with different failure modes, and the cost of
the extra clamp is a comparison.

## Voice forms

| You say | What happens |
|---|---|
| "set volume to 7" | level 7 on a 1-10 scale |
| "set volume to 40%" | 40 percent |
| "set volume to 65" | 65 percent (a bare number above 10 can only be a percentage) |
| "volume up" / "volume down" | one step |
| "volume up by two" / "volume down by 3" | that many steps |
| "turn the volume up" / "louder" / "quieter" | one step |
| "what's the volume" | the current level, and it changes nothing |

Every reply is one short line, spoken in the terms you used: "Volume 7 out of 10." for the
scale, "Volume 40 percent." for a percentage.

### The 1-10 scale spans what you can hear

Level 1 is the floor and level 10 is full volume. Mapping level 1 to "10 percent" instead
would make the bottom of the user's own scale effectively silent, which is the failure the
issue is about. So the scale is stretched over `[floor, 100]`, not `[10, 100]`.

### Deliberately not matched

Bare **"turn it up"** and **"turn it down"** are not volume commands, even though they
probably mean the device. "It" could be music playing on the same device, and the Gateway
cannot tell the difference. These requests go to the assistant instead. A mis-heard
bridge-local command that changes the room is worse than the assistant taking an extra
second. `turn the volume up` and `volume up` both work, because neither leaves the object
ambiguous.

### What must happen after a fall-through, and what must not

Handing a request to the assistant is only safe if the assistant answers the request that was
actually made. A volume request the matcher declines is still a volume request: it is about this
device's own playback level, and it is never satisfied by changing another device in the house.

That is not hypothetical. On 2026-09-25 a spoken **"volume five"** was transcribed with the word
*volume* lost, so the matcher correctly declined it and let it through — and the assistant then set
the **thermostat** to 65, a device the person never named, while the volume never changed at all.
The transcript had arrived in the middle of a thermostat conversation, so the model reached for the
device that was in context instead of the device that was asked about. It had also invented the
value 65 on the previous turn, offered it back as "did you mean 65?", and then read the person's
repeat of their own request as agreement to that guess.

The clauses that close that chain are in `VOICE_INSTRUCTIONS`: a device is never changed unless the
person named it, a clarifying question may never introduce a number the person did not say, a repeat
is the same request rather than a yes, and a garbled or fragmentary utterance never becomes a state
change. `scripts/test_voice_device_scope.py` pins them offline. Note what the matcher's conservatism
now implies: when a volume request is mis-heard badly enough to lose the word *volume*, the honest
outcome is that the assistant asks rather than acts, and the person says it again more clearly. That
is the intended trade — an extra exchange instead of a changed thermostat.

## Alarms override upward

The decision, 2026-09-25: **an alarm may override upward to a minimum alarm level, so
an alarm is always audible.** An alarm at a level too quiet to notice is useless.

The override is **upward only**. A room already louder than the minimum is left exactly
alone, because lowering a level she chose would make the override a surprise rather than a
safeguard.

The raised level is held for the clip's own measured length, plus a margin for the device's
buffering, because `play_wav` returns when the audio has been *sent*, not when it finishes.
Restoring immediately would cut the alarm back down before anyone heard it, making the
override cosmetic. The hold is capped so a long clip cannot pin the volume.

## Calibration: the two things that cannot be reasoned about

Neither of these can be established from a desk, and the issue is explicit that they are
measured, not assumed:

1. **Which ALSA control the speaker follows.** The codec exposes `PCM`, `Line`, `HP`,
   `HP DAC` and `HPCOM`. **The name is not evidence.** Only listening is.
2. **The audible floor.** The lowest level you can actually hear. A percentage that is
   effectively silent is not a floor, it is a device that has stopped answering.

### Procedure, on the satellite

```bash
# 1. What controls exist
python3 calibrate-volume.py --enumerate

# 2. Which one drives the speaker. Plays a loud then a quiet tone through each candidate
#    and asks you what you heard.
python3 calibrate-volume.py --probe

# 3. Where the floor is. Sweeps down and asks when the tone disappears.
python3 calibrate-volume.py --floor --control '<the control you found>'

# 4. What is recorded now
python3 calibrate-volume.py --show
```

The script is stdlib-only and imports nothing from this repository, on purpose: it has to
run on an embedded device with no dependency chain. Copy it across on its own.

### The measurement, taken 2026-09-25 on this device

| Value | Result |
|---|---|
| `control` | **`PCM`** |
| `floor_percent` | **50** |
| `alarm_min_percent` | **50** |

**Which control.** The device's own microphone identified it objectively: moving `PCM` from
100% to 5% changed the recorded level by **89%**, so the speaker plainly follows it. `Line`
also affects the output (a 30% swing) and is deliberately left at the HAT's value of 78% as
part of the fixed gain path. `HP`, `HP DAC` and `HPCOM` had **no measurable effect** (98-100%
unchanged) despite names that suggest they would, which is exactly why the issue said not to
guess from the name.

**The floor, by ear.** Six descending tones, 2.5 s each: 100%, 70%, 50%, 35%, 25%, 15%.
Result: 100% unmistakably loud, **50% the softest level that could be heard at all**, and
35/25/15 entirely inaudible.

**Why the microphone was the wrong instrument for this part.** It put the floor near 20-25%,
because it sits centimetres from the speaker and hears near-field output that a person across
the room cannot. It is excellent for identifying the control and useless for the floor. Issue
#10 asks for the floor "by ear" for precisely this reason, and the by-ear figure is the one
that is stored.

### Two scales, both spanning what the hardware can do

There are two numbers for volume, and confusing them is the whole difficulty.

**Raw** is what the ALSA control takes. On this device the floor is raw 50, so raw runs 50-100
and nothing below 50 is audible.

**User** is what a person speaks and what the configuration plane carries: 1-100, where 1 is
the quietest the hardware can go and 100 is full volume.

Both spoken scales are positions on the user range, so they agree by construction:

| User | Raw | Say | Means |
|---|---|---|---|
| 1% / level 1 | 50 | the quietest | the measured floor |
| 50% / level 5 | 75 | halfway | halfway up what the hardware can do |
| 100% / level 10 | 100 | full | full volume |

The 1-10 scale is simply the user range sampled ten times, not a second mapping that has to be
kept in step with the first.

**This was a correction.** The first version mapped the percentage scale onto raw directly, so
with a floor of 50 every value from 1% to 50% meant the same thing and was reported back as a
clamp - half the percentage scale was dead. The point: both scales should exist *within*
the measured range, so "volume 30%" is 30% of what this hardware can do.

The configuration plane carries the user scale too, deliberately. A fleet API that reported raw
ALSA numbers would be meaningless across devices with different measured floors.

**One consequence, handled.** The user->raw->user round trip passes through an integer ALSA
percentage, so it shifts by one for 49 of the 100 values. `playback_volume` therefore carries a
`round_trip_tolerance` of 1 in the configuration schema, or those settings would report
permanent drift and never converge - which teaches whoever reads the fleet view to ignore drift
altogether. The setting is converged, at the resolution the hardware can express.

**Alarm minimum 50%, on the user scale.** The decision: alarms may override upward so an alarm
is always audible. User 50% maps to raw 75 here, comfortably above the raw 70 that the sweep
showed as clearly audible, and well above the raw 50 floor where the device can only just be
heard. Stored on the user scale so the same number means the same thing on hardware with a
different floor.

### Where the measurement is stored

Neither copy is committed, because `state/` is gitignored, which is right for runtime
measurement.

| Where | Path |
|---|---|
| Gateway | `state/volume-calibration.json` |
| Device | `~/.local/state/hermes-voice-satellite/volume.json` (the device user's home) |

Pin `VOICE_VOLUME_CALIBRATION` on the device so both resolve the same file (see above).

### Before calibration, and on new hardware

With no calibration file both sides fall back to conservative **placeholders** (floor 25%,
alarm minimum 60%, control `PCM`) and log a line at startup saying so, because a placeholder
nobody mentions becomes permanent.

They are **not portable**. The 50% figure is this HAT, this codec and this room. A different
speaker, a different `Line` gain or a different enclosure will measure differently, and a
floor carried over from another device is a guess wearing a measurement's clothes.

The placeholders are safe by construction: above zero, so the device stays audible even before
anyone has listened.

### The calibration path must be pinned, and why

**The satellite runs its services as `root`.** None of the units sets `User=` — not this one,
not `wyoming-satellite`, not `wake-shim`, not `2mic_leds`. So `Path.home()` is `/root` for the
service, while a person running the calibration is logged in as the device user and gets
their own home.

Unpinned, those two paths differ, and the failure is silent and total: the measurement is
written where the running service never looks, so the floor stays at its placeholder and the
calibration appears to have done nothing at all.

Pin it explicitly in the device's bootstrap file, on **both** sides:

```
VOICE_VOLUME_CALIBRATION=/home/<satellite-user>/.local/state/hermes-voice-satellite/volume.json
VOICE_DEVICE_CONFIG_STATE=/home/<satellite-user>/.local/state/hermes-voice-satellite/config.json
```

Replace `<satellite-user>` with the device account before saving. These are literal absolute paths:
`EnvironmentFile=` does not expand `$HOME` (and the service's home would be root's anyway).

`VOICE_VOLUME_CALIBRATION` is the variable both the calibration script and
`pi/volume_control.py` honour, so the writer and the reader resolve the same path regardless
of who runs them. `scripts/test_volume_calibration.py` pins that agreement, including
evaluating the reader in a subprocess so its module-level constant is built with the variable
actually set. Without that check a measurement could sit on disk being correctly ignored.

Create the directory as the device user (mode `0700`) so the person can write the
measurement and `root` can read it.

### After calibrating

The calibrated values are module constants, because `clamp()` is on the hot path for every
announcement, timer and alarm and must not stat the filesystem to decide whether it is
allowed to be silent. So a calibration change takes effect through **either** a Gateway
restart **or** `bridge.volume.reload_calibration()`.

Doing neither leaves the measured floor on disk being ignored, which looks exactly like the
feature being broken. `scripts/test_volume_calibration.py` pins that the reload is what makes
a measurement real.

## Where the level lives

Speech is rendered on the Gateway, but **playback happens on the satellite**, so the number
that matters is the Pi's ALSA level for the output card. Our own TTS, the connected tone,
the done tone and announcements all take that one path, which is why one control governs
everything.

Persistence is `alsactl store`, because **an ALSA level resets on reboot without it**. The
device adapter calls it on every change; `scripts/test_device_apply.py` pins that it does.

## What the device can and cannot apply yet

`pi/device_apply.py` is the hardware adapter, injected into the configuration agent.

- **`playback_volume` applies.** Verified against a fake `amixer`; not yet on real hardware.
- **`wake_word`, `wake_threshold`, `mic_gain`, `led_brightness` are staged, not applied.**
  The adapter reports `degraded` and names them. It does not return a healthy status it did
  not earn, which is the whole reason the configuration plane distinguishes desired from
  reported state.

## Verification status

Honest accounting, because the device is off-site.

**Verified offline (279 checks across six suites):**

- all four voice forms parse, resolve and answer correctly;
- "set volume to 0", "set volume to 0%", "volume down by 9", and every other request from
  every starting level cannot produce a level below the floor;
- a stored zero or sub-floor value is raised on load rather than resumed;
- the device adapter never sends 0% to `amixer`, and reports a floor adjustment instead of
  applying it silently;
- an alarm raises a quiet room, never lowers a loud one, holds for the clip's measured
  length, and still plays if the raise fails;
- a failed application is never reported as success.

**NOT verified — needs the device:**

- which ALSA control the speaker actually follows;
- the audible floor, by ear;
- that the level survives a **real reboot** (a service restart is not the same test);
- that `alsactl store` succeeds on this image;
- the real spoken round trip, end to end, through the microphone.

Every one of those is on the calibration script, and none of it is claimed here.

## Files

| File | Role |
|---|---|
| `bridge/volume.py` | parsing, the scale, the floor, the alarm override, replies |
| `pi/volume_control.py` | the device's own `amixer`/`alsactl` path and its own floor |
| `pi/device_apply.py` | the configuration plane's hardware adapter |
| `scripts/calibrate-volume.py` | the on-device measurement (standalone) |
| `scripts/test_volume.py` | parsing, scale, floor, replies, the no-silence property |
| `scripts/test_volume_integration.py` | the Gateway path, including a stored zero on load |
| `scripts/test_device_volume.py` | the device `amixer` path |
| `scripts/test_device_apply.py` | the apply adapter, and its honesty about partial application |
| `scripts/test_alarm_volume.py` | the alarm override, and its hold duration |
| `scripts/test_volume_calibration.py` | writer-to-reader format contract, and safe fallbacks |
