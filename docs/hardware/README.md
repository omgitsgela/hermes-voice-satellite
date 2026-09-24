# Hardware: supported devices, their enclosures and their bring-up

One file per supported ReSpeaker model. The point is that someone rebuilding this device, or adding a
new one, has the wiring, the software configuration and the gotchas in one place instead of
rediscovering them.

| Model | Status | Entry |
|---|---|---|
| Seeed ReSpeaker 2-Mics Pi HAT **V2.0** (TLV320AIC3104) | **In service, built and tested** | [`seeed-2mic-hat-v2.md`](seeed-2mic-hat-v2.md) |
| ReSpeaker 4-Mic Array for Raspberry Pi | Discontinued, not owned | see issue #7 |
| Canonical supported units (Pi HAT, ESP32 devices) | see the canonical list | `../supported-devices.md` |
| Integrated ESP32-class devices (reSpeaker Lite kit, XIAO variants) | Path B, blocked on the architecture decision in #11 | see issue #7 |

USB-only microphones and arrays are **not supported**: their audio and their controls both belong to a
vendor SDK on a host we do not own. See the exclusion table for the reasoning.

## How to add a model

1. **Run `scripts/device-check.py` on the satellite host before starting any services.** A card that is
   already in use cannot be inspected, and the script says so rather than guessing. Keep its output with
   the entry, as `seeed-2mic-hat-v2.md` does.
2. Record the identity: the ALSA card name, the codec, and the device-tree overlay that enables it.
3. Record the enclosure and how audio physically gets out. On a Pi Zero 2 W there is no audio output of
   its own, so the device must supply one, whether a speaker header, a 3.5 mm jack, or a separate speaker.
4. Record the exact service arguments, and why each one is there.
5. Record the wake behaviour: which wake model, the threshold, and how it was measured.
6. Note what the model *could* do that we have not switched on. Several useful flags are unused today.

## Shared software steps, whatever the model

These are the parts that do not change per model. The full detail is in
`../owning-the-satellite-stack.md`.

1. Flash **Raspberry Pi OS 64-bit**. 64-bit is not optional: audio enhancement and local wake word
   detection require it, and the upstream tutorial says so explicitly.
2. Install the audio driver. On the HAT path that is Seeed `seeed-voicecard`, which is **DKMS-installed**
   here, so a kernel upgrade rebuilds it automatically. Confirm with `/usr/sbin/dkms status` (note the
   full path: `/usr/sbin` is not on the PATH in a non-login SSH, and `which dkms` will mislead you).
3. Install the satellite at the pinned revision, with our core patch, from the vendored source in
   `pi/vendor/native-satellite-13bb024/`.
4. Copy the wake model into the custom model directory and point the wake service's
   `--custom-model-dir` at it. The model is named by its filename stem.
5. Install the units and drop-ins from `pi/systemd/`, then `daemon-reload`. Start order: LED service,
   wake engine, shim, then satellite. Restarting the shim or the LED service **bounces the satellite**,
   and a stopped satellite does not come back on its own.
6. Verify with `scripts/device-check.py`, then by voice: wake word fires, an answer plays, the LEDs
   behave, and the startup tone plays once per boot and not per restart.

## ALSA naming, which is where new models usually bite

Our satellite opens capture and playback as `plughw:CARD=<card>,DEV=0`. Prefer `plughw:` over `hw:`
for the same reason upstream does: `plughw` lets ALSA convert sample rate and format, so a device that
cannot do 16 kHz mono natively still works. Find the names with `arecord -L` and `aplay -L`.

## Satellite flags: what we pass, and what we do not use yet

Confirmed present at our pinned revision, from `wyoming_satellite/__main__.py`:

| Flag | Range or effect | Our position |
|---|---|---|
| `--wake-refractory-seconds` | Seconds before the wake word can fire again | Leaving the default. Worth revisiting with the false-activation work in #4 |
| `--mic-auto-gain` | 0 to 31 dBFS | **Unused.** Plausible fix for the far-field case that drives the long endpointer wait |
| `--mic-noise-suppression` | 0 to 4, where 4 may distort | **Unused.** Worth testing in the room that produces the 15-second waits |
| `--mic-volume-multiplier` | Multiplies captured samples | Unused; the HAT's preamp is set in hardware instead |
| `--snd-volume-multiplier` | Multiplies played samples | **Unused, and the obvious lever for voice volume control** in #10 |
| `--timer-finished-wav` and `--timer-finished-wav-repeat <repeats> <delay>` | Timer tone, optionally repeated | **Unused, and it means the satellite can drive timers itself.** See issue #5 |
| `--vad` | Streams only after speech detection | Deliberately unused: unnecessary with local wake word detection, and the modem-style VAD path caused trouble before |

## One thing to check on every rebuild

The **underrun count**. The Pi already shows occasional playback underruns, and it is the first symptom
of a power, Wi-Fi or CPU problem on this device. `journalctl -u wyoming-satellite | grep -c underrun`.
