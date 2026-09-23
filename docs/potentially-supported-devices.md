# Potentially supported microphone devices

Shortlist and evaluation plan, 2026-09-23. **Nothing here has been tested on our hardware.** Every
entry is compiled from Seeed's own product pages and wiki pages, read on 2026-09-23, and the verdicts
are predictions to be confirmed when a unit arrives. Prices are USD list prices as displayed, and
they vary by warehouse and by day, so verify before buying.

The point of this file: Angela intends to buy several of these and test them, so the reasoning is
written down before the boxes arrive.

## Our baseline, measured

| | Current device |
|---|---|
| Capture | Seeed ReSpeaker 2-Mics Pi HAT (I2S HAT), card `seeed2micvoicec`, codec `tlv320aic3x`, opened as `plughw:CARD=seeed2micvoicec` |
| Driver | Seeed `seeed-voicecard` 0.3, built against kernel `6.12.109+rpt-rpi-v8`, **not DKMS**, so a kernel upgrade removes the card until it is rebuilt |
| Playback | the HAT's **speaker header**, which drives a speaker directly off the codec's amplifier. The same HAT also exposes a 3.5 mm jack; we are not using it. A Pi Zero 2 W has no analogue output of its own, so this HAT is what gives us sound at all. |
| LEDs | 3 x APA102 on `/dev/spidev0.0`, driven by our own service |
| Host | Pi Zero 2 W, 416 MB RAM, ~171 MB available, Debian 12 arm64, 64-bit |
| Wake word | `hey_hermes`, openWakeWord, on the Pi |

## What "compatible" means here

A device has to satisfy all of these to be a drop-in:

1. **It appears as an ALSA sound card on Linux**, capturing at 16 kHz mono. USB Audio Class or a
   Raspberry Pi I2S HAT with a Linux driver both qualify. A board whose host is its own
   microcontroller does not, because our stack is Linux.
2. **It gives us an audio output**, or we have to solve output separately. The Pi Zero 2 W has no
   analogue output of its own, so the device has to supply one. A 3.5 mm jack and an amplified
   speaker header are equally acceptable; our current HAT offers both and we use the speaker header,
   which is the pattern a replacement should follow. A capture-only device means adding a USB
   speaker or similar.
3. **Its LEDs are addressable by us.** This is the sharpest constraint we have: our LED service
   drives **APA102 over SPI**. Every device below uses **WS2812**, which is a different chip on a
   different bus, so none of them can reuse that service and all of them need new LED code.
4. **Bonus: on-chip AEC and beamforming.** See `docs/feature-roadmap.md`, where acoustic echo
   cancellation is the biggest feature gap we have. Hardware can close it for free.

## Tier 1: predicted drop-in

Factory USB Audio Class, driverless on Linux, with analogue output of its own.

| Device | SKU | Price | Chip | Mics | LEDs | Output |
|---|---|---|---|---|---|---|
| reSpeaker XMOS XVF3800 (bare) | 101991441 | $54.90 | XVF3800 + TLV320AIC3104 | 4, circular, ~5 m | 12 x WS2812 + mute button | 3.5 mm jack + 5 W speaker connector |
| reSpeaker XMOS XVF3800 with Case | 114993701 | $62.99 | XVF3800 + AIC3104 | 4, circular | 12 x WS2812 + mute button | 3.5 mm + 5 W |
| reSpeaker XMOS XVF3000 with Case | 107990193 | $70.99 | XVF3000 + AIC3104 | 4, circular | 12 x RGB | 3.5 mm |
| reSpeaker Flex XVF3800 Circular-4 (bare) | 100005504 | $57.99 | XVF3800 + AIC3104 + TPA3139D2 | 4, circular 44 mm | power LED only | 3.5 mm + 10 W connector |
| reSpeaker Flex XVF3800 Linear-4 (bare) | 100099135 | $57.99 | XVF3800 + AIC3104 + TPA3139D2 | 4, linear 33 mm, ~180 degrees | power LED only | 3.5 mm + 10 W connector |
| reSpeaker Lite (bare USB) | 107990273 | $26.99 | XMOS XU316 | 2, ~3 m | 1 x WS2812 + USR and mute buttons | 3.5 mm + 5 W connector |

**Recommended first purchase: the bare XVF3800 at $54.90.** It is the cheapest way to get the whole
package: four mics, on-chip AEC/beamforming/noise suppression, a USB audio class interface with no
driver at all, twelve programmable LEDs, a mute button, and its own analogue output so the Pi Zero's
missing jack stops mattering. It is also the direct successor to the classic ReSpeaker Mic Array
v2.0, which is the shape of device this software ecosystem was built around.

**Cheapest useful test: reSpeaker Lite at $26.99.** Two mics instead of four, one WS2812, still USB
class compliant with an output jack. Good for a baseline comparison without much money.

**The Flex pair** are the same electronics on a split board where the microphone array is on an FPC
away from the core. Worth one unit if the interesting question is placement, mounting, or getting
mics away from the Pi's own electrical noise. They have no programmable LED array.

## Tier 2: works, but needs a firmware switch first

These ship with a XIAO ESP32S3 as the host, in I2S mode, so as delivered they are **not Linux sound
cards**. Seeed documents converting the XMOS chip to USB firmware, after which they behave like the
tier 1 devices. Buying the bare version is strictly better for us.

| Device | SKU | Price | Why it is tier 2 |
|---|---|---|---|
| XVF3800 with XIAO ESP32S3 | 114993700 | $66.99 | factory I2S/MCU mode; USB after firmware switch |
| XVF3800 with XIAO ESP32S3, case | 114993702 | $67.99 | same, costs more than the bare USB case version |
| reSpeaker Lite Voice Kit | 110061601 | $30.99 | factory ESPHome/MCU target; USB firmware available |
| Flex XVF3800 Circular-4 with XIAO | 100070894 | $60.90 | factory I2S; USB after switch |
| Flex XVF3800 Linear-4 with XIAO | 100026178 | $60.90 | factory I2S; USB after switch |

The only reason to buy one is if we deliberately want to test the ESPHome path, which is the stack
the successor project uses and which is a different architecture, not a different driver. That is a
separate experiment and it does not involve our bridge.

## Tier 3: not usable as a microphone here

**Sound Event Detection Module D1** (SKU 100049596, $18.90) is an XMOS XU316 event classifier, not an
audio device. It listens for baby cry, glass break, gunshot, smoke alarm patterns and snoring, and
sends only the classification and timestamp over USB serial or UART. It never sends raw audio, so it
cannot be a Wyoming microphone, and it has no playback path.

Worth remembering as a **separate idea** rather than a failure: something that can hear a glass break
in the house and tell the assistant to announce it is genuinely useful, and it would hang off the
bridge as an event source, not as an audio device.

## I2S HATs, and what is still buyable

The driver we already run is more capable than our HAT needs: `pi/vendor/seeed-voicecard-0.3` ships
device-tree overlays and asound state for **2, 4, 6 and 8 microphone** variants. Any ReSpeaker I2S HAT
matching one of those should work with the driver already on the device, with no new code and no USB
adapter, which makes HATs the cheapest experiments of all.

| Older HAT | Status on 2026-09-23 | Price |
|---|---|---|
| ReSpeaker 2-Mics Pi HAT | **Still listed** (SKU 107100001) | $13.99 |
| ReSpeaker 4-Mic Array for Raspberry Pi | **Out of stock and discontinued** (SKU 103030216) | $24.90 historically |

Caution on the still-listed 2-Mics HAT: the current listing identifies **V2.0 with a TLV320AIC3104**
codec. Our deployed HAT reports a `tlv320aic3x` codec, and the same Linux driver family covers both,
so it is probably the same experience, but this is exactly the kind of difference that should be
confirmed by testing a new unit rather than assumed.

## What each purchase costs us in work

- **ALSA selection:** minutes. Change the capture and playback device names and re-test.
- **Channel choice:** an opportunity. The XVF3800 exposes processed channels, so we can point
  speech-to-text at a beamformed channel instead of raw mic audio, which may improve both wake and
  transcription in a noisy room.
- **LED re-integration:** the real work. Every device here uses WS2812, not our APA102/SPI. The
  existing LED service cannot drive them, so each candidate needs either a small new controller or a
  decision that this device runs without our LED language.
- **On a Pi Zero 2 W:** the single USB data port is shared, so a USB microphone needs an OTG adapter
  and leaves the power port for power. Worth having a powered hub on the bench.
- **Audio server:** a USB device is unaffected, but if we ever add PipeWire for software AEC, that is
  a change to a device with 171 MB of headroom, and the USB arrays make it unnecessary.

## Test checklist for each unit when it arrives

1. `arecord -l` and `aplay -l`: does it appear, and does it claim 16 kHz mono capture.
2. Record and play back a known phrase at a fixed distance, and compare it with the same phrase on
   the 2-mic HAT. Same sentence, same distance, same room.
3. Wake word: fire it ten times from three metres and count the misses.
4. **Count false activations over an hour**, which is the number we actually care about, and the same
   number the wake-word library is judged on.
5. **The echo test:** start a long answer and talk over it. Does the device hear itself or answer its
   own speech? This is the single most valuable measurement here, because on-chip AEC would let us
   delete our self-trigger workarounds.
6. Noisy-room test: the 15-second endpointer wait is our known weak spot, so compare how long an
   utterance takes to close with and without the new device.
7. Playback: volume, quality, and whether the analogue output is loud enough for the room.
8. LEDs: what can we actually drive, and over what interface.
9. Thermal and power: temperature and `get_throttled` after an hour, against the current baseline of
   59.6 C and `0x0`.

Record the results in a table in this file as they come in, including the ones that lose. A failed
candidate is still a result.

## How we will support these devices

Angela's direction, 2026-09-23:

> We will maintain our own software stack to support the respeaker devices, including the LED
> drivers, and update code as needed to keep within kernel updates as they happen. It's going to be a
> lot of work, but we can do it. I'll buy a bunch of these devices so we can practically test them.

That makes this project the owner of the device layer, not a consumer of it. Concretely, that means:

**One code path, several devices.** The differences between candidates are limited and known: the
ALSA device name, which channel to capture, the playback device, the LED chip and bus, and whether
there is a button. None of that belongs in `if device == ...` branches. The clean shape is a small
**device profile** per unit (capture device, playback device, channel, LED type, button), with the
services reading the profile instead of hard-coding names. Worth building when the second device
arrives and the real differences are visible, rather than guessing now.

**The LED layer is the real work.** Our current service drives APA102 over SPI. Every candidate here
uses WS2812, and the XVF3800's twelve LEDs are driven by the XMOS chip over USB, not by a GPIO bus at
all. So this becomes a small driver layer behind one interface, with an implementation per chip, and
the state machine (boot pulse, wake, working, follow-up, red) staying exactly as it is. The state
machine is the valuable part and it is already written and tested.

**Kernel updates.** The Seeed driver is not installed with DKMS, so a kernel upgrade silently removes
the sound card. The driver source ships a `dkms.conf`, which means converting the install to DKMS
would make it rebuild automatically on every kernel change. That is the single highest-value piece of
maintenance work for keeping this stack alive, and it is a small job.

**A repeatable device check.** The checklist above should become a script that reports what a unit
actually is: which ALSA cards exist, what capture formats they claim, which LED bus is present, and
whether a button exists. Run it once per device, before writing any code for that device, and keep
the reports in this folder. That turns "works on my desk" into a record.

**Proof of the replacement path.** The vault already records that our HAT is the **V2.0 revision with
a TLV320AIC3104** codec, enabled by `dtoverlay=respeaker-2mic-v2_0`, and that most guides describe
the older V1.x with a WM8960. The 2-Mics HAT still on sale is listed as V2.0/AIC3104, which matches
what we run, so re-buying the same HAT is a like-for-like replacement for the current device and the
cheapest way to have a spare. Confirm it by testing one, but expect it to match.
