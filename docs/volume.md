# Volume control

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
| `bridge/volume.py` `clamp()` | the bridge's choke point; raises anything below the floor |
| `pi/volume_control.py` `AlsaVolume.clamp()` | the device's own choke point, so a bug or a bad desired value on the host still cannot produce a deaf device |
| `scripts/calibrate-volume.py` | refuses to record a floor of 0, and validates on read |

Each of the three is pinned by its own tests. Three is not redundancy for its own sake: the
bridge and the device are different machines with different failure modes, and the cost of
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
probably mean the device. "It" could be music playing on the same device, and the bridge
cannot tell the difference. These requests go to the assistant instead. A mis-heard
bridge-local command that changes the room is worse than the assistant taking an extra
second. `turn the volume up` and `volume up` both work, because neither leaves the object
ambiguous.

## Alarms override upward

Angela's decision, 2026-09-25: **an alarm may override upward to a minimum alarm level, so
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

### Until it is calibrated

The bridge and the device both use **placeholder** values (floor 25%, alarm minimum 60%,
control `PCM`, card `seeed2micvoicec`). These are conservative, not measured. Both sides log
a line at startup saying so, because a placeholder that nobody mentions becomes permanent.

The placeholders are safe by construction: they are above zero, so the device stays audible
even before anyone has listened.

### After calibrating

The calibrated values are module constants, because `clamp()` is on the hot path for every
announcement, timer and alarm and must not stat the filesystem to decide whether it is
allowed to be silent. So a calibration change takes effect through **either** a bridge
restart **or** `bridge.volume.reload_calibration()`.

Doing neither leaves the measured floor on disk being ignored, which looks exactly like the
feature being broken. `scripts/test_volume_calibration.py` pins that the reload is what makes
a measurement real.

## Where the level lives

Speech is rendered on the bridge, but **playback happens on the satellite**, so the number
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
| `scripts/test_volume_integration.py` | the bridge path, including a stored zero on load |
| `scripts/test_device_volume.py` | the device `amixer` path |
| `scripts/test_device_apply.py` | the apply adapter, and its honesty about partial application |
| `scripts/test_alarm_volume.py` | the alarm override, and its hold duration |
| `scripts/test_volume_calibration.py` | writer-to-reader format contract, and safe fallbacks |
