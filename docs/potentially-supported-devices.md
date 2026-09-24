# Supported devices: what counts, and what is in each class

Rewritten 2026-09-23 to match Angela's hardware schema. Prices are USD list prices as displayed when
compiled and shift by warehouse; SKUs are for verifying rather than guessing.

## What counts as supported

A device has to be one of exactly two shapes:

1. **A Raspberry Pi plus an add-on board (a HAT).** The Pi is the host, the HAT supplies the microphones
   and the control surface, and our own software drives the LEDs over SPI. This is what we run today.
2. **An integrated ESP32-class device**, with its own processor and flashable firmware, where the device
   is the thing rather than a peripheral of one.

**Explicitly not supported: USB-only microphones and arrays.** Their audio is USB, their LEDs and buttons
are driven through the vendor's own SDK over USB, and there is no board we own. They are peripherals for a
host that someone else's software controls, which is the wrong shape for a device we intend to provision,
update and own end to end.

## Path A: Pi plus HAT (what we run, and the cheapest good buys)

| Device | SKU | Price | Status |
|---|---|---|---|
| Seeed ReSpeaker **2-Mics Pi HAT V2.0** (TLV320AIC3104) | 107100001 | $13.99 | **In service.** Full entry: [`hardware/seeed-2mic-hat-v2.md`](hardware/seeed-2mic-hat-v2.md) |
| ReSpeaker 4-Mic Array for Raspberry Pi | 103030216 | $24.90 | **Discontinued at Seeed.** The driver supports 4-mic arrays, so equivalents from other brands are worth finding |
| ReSpeaker 2-Mics Pi HAT, other brands | n/a | varies | Upstream's own tutorial points at a Keyestudio equivalent of our board, so clones are entirely normal here |

https://www.seeedstudio.com/ReSpeaker-2-Mics-Pi-HAT.html
https://www.seeedstudio.com/ReSpeaker-4-Mic-Array-for-Raspberry-Pi.html

**Why this path wins on cost and effort:** the Seeed `seeed-voicecard` driver we already run is
DKMS-installed and ships device-tree overlays and mixer state for **2, 4, 6 and 8 microphone** arrays. A
HAT matching one of those needs no new driver, no kernel work and no vendor SDK. Our LED service pattern
already exists for it, and the spare for the device we run costs $13.99.

## Path B: integrated ESP32-class devices

| Device | SKU | Price | Note |
|---|---|---|---|
| reSpeaker Lite Voice Assistant Kit (ESPHome target) | 110061601 | $30.99 | The clearest example of the class: an MCU device, not a peripheral |
| reSpeaker XVF3800 with XIAO ESP32S3 | 114993700 | $66.99 | Array plus ESP32S3 host on one board |
| reSpeaker XVF3800 with XIAO ESP32S3, case | 114993702 | $67.99 | Same, cased |
| reSpeaker Flex XVF3800 Circular-4 with XIAO | 100070894 | $60.90 | Split array on an FPC plus ESP32S3 host |
| reSpeaker Flex XVF3800 Linear-4 with XIAO | 100026178 | $60.90 | Same, linear array |

https://www.seeedstudio.com/ReSpeaker-Lite-Voice-Assistant-Kit-p-5929.html
https://www.seeedstudio.com/ReSpeaker-XVF3800-4-Mic-Array-With-XIAO-ESP32S3-p-6489.html
https://www.seeedstudio.com/ReSpeaker-XVF3800-With-Case-XIAO-ESP32S3-p-6628.html
https://www.seeedstudio.com/reSpeaker-Flex-XVF3800-Circular-4-with-XIAO-ESP32S3-p-6739.html
https://www.seeedstudio.com/reSpeaker-Flex-XVF3800-Linear-4-with-XIAO-ESP32S3-p-6736.html

**Settled 2026-09-23: we ship our own firmware on these devices, speaking Wyoming straight to the
bridge.** So their value to us is not ESPHome support, it is that the microphone array, the LEDs and the
buttons are documented and already driven from a microcontroller by someone else, which we can read as a
reference. No ESPHome, no Linux Voice Assistant, no adoption of another stack: the device talks to our
bridge exactly as the Pi does.

The project's size makes this reasonable. Wyoming is JSON lines plus PCM audio, and 16 kHz mono is about
32 KB/s, small enough for a microcontroller and trivially small over Wi-Fi.

## Watchlist: matches the schema, not usable yet

| Device | Why it is on the list |
|---|---|
| reSpeaker Clip (wearable recorder) | Fits the integrated-device class on paper: its own processor, open-source firmware, and programmable button logic and display content, which is a control surface we could write ourselves. Blockers today are that the firmware and SDK downloads are still marked coming soon with the wiki in draft, and that it is built as a recorder for transcription and summarisation rather than as a conversational device. It also documents its own app, the SenseCraft Voice App, alongside the open SDK, so the answer to "is there an app or do we write our own" is currently both. Revisit when the SDK ships. |

https://www.seeedstudio.com/respeaker-clip-wearable-ai-recorder.html

## Excluded, and why (so the same question is not re-litigated)

| Device | Why it is out |
|---|---|
| reSpeaker XMOS XVF3800 (bare, USB) | USB audio with an SDK-driven control surface. No board we own |
| reSpeaker XMOS XVF3800 with Case (USB) | Same |
| reSpeaker XMOS XVF3000 with Case (USB, the classic Mic Array v2.0 shape) | Same |
| reSpeaker Flex XVF3800 Circular-4 (bare USB) | Same |
| reSpeaker Flex XVF3800 Linear-4 (bare USB) | Same |
| reSpeaker Lite (bare USB) | Same |
| Sound Event Detection Module D1 | Not an audio device at all: an event classifier that reports classifications over USB serial. Its own idea, tracked separately, not a satellite component |

The excluded devices are not bad hardware. They are good audio with someone else's control plane, and they
would leave us maintaining a vendor SDK just to change the colour of a light.

## What to buy, in order

1. **A spare ReSpeaker 2-Mics Pi HAT, $13.99.** The same V2.0/AIC3104 revision we run, so it is a
   like-for-like replacement and a fair baseline for any comparison.
2. **A 4-mic or 6-mic I2S HAT from any brand matching the driver's overlays**, if one can be found at a sane
   price. Same path, no new software, better far-field pickup in principle.
3. **For firmware development, the reSpeaker Lite kit at $30.99.** Cheapest route to a flashed device
   speaking our protocol, fewest unknowns, and it has a small LED and two buttons to exercise the control
   path. The XVF3800 with XIAO at $66.99 is the natural second unit, since it brings on-chip echo
   cancellation, which is our biggest feature gap.


## Verified 2026-09-23, while shopping the listings

### Which Raspberry Pi models the HAT actually runs on

From the driver's own README, the officially supported set is **Raspberry Pi 3 (all models), Raspberry Pi 4
(all models), Raspberry Pi Zero and Zero W**, on **32-bit Raspberry Pi OS**, with **64-bit marked
experimental**. The 2-Mics HAT is a 40-pin board, so it fits anything with a 40-pin header, but fitting is
not the same as working:

- **Raspberry Pi 5 is not supported.** It is not on the list, and the Pi 5's RP1 chip changed the audio and
  GPIO plumbing. Treat it as unknown until we test one, not as a safe buy.
- **Pi Zero and Zero W (the originals) are supported by the driver but too weak for our software**, which
  needs a 64-bit OS and enough RAM to run a wake word. The floor for this project is a **Pi Zero 2 W**.
- Our own Zero 2 W is not on Seeed's list either and works fine, so the list is conservative rather than
  complete. For a buyer, quote the supported list and add the Zero 2 W as the known-good build.

### Pricing trap on the Seeed listings

The **"10+:" figure on a product page is a bulk price, not the unit price.** The 2-Mics HAT shows $11.99 at
10 or more against $13.99 singly. Any list sent to a buyer should use single-unit prices, which is what
`What to buy` above does.

### reSpeaker Lite Voice Assistant Kit, full kit variant

The full kit exists in **two variants**: with the XIAO ESP32S3 pre-soldered, or without it. Only the
pre-soldered one has a processor. Order the wrong one and there is nothing to flash.

### reSpeaker XMOS XVF3800 with XIAO ESP32S3

The listing confirms more than we credited, and it makes this the target device for our own firmware:

- **Dual modes, I2S or USB.** The I2S path means the ESP32 reads the array directly, so no USB firmware
  conversion is needed in the MCU direction.
- **On-board AEC, AGC, DoA, VAD, dereverberation, beamforming and noise suppression**, with 360 degree
  far-field pickup to 5 metres.

That is the whole hard part of voice capture done on the array and handed to the microcontroller as clean
audio. It closes the echo cancellation gap (#1) in hardware rather than in our code, which is why this is
the device to aim for rather than the cheapest one.

### reSpeaker Clip

Not supported, and it should not go on a buyer list yet. Its firmware, SDK and datasheet are all still
marked **"coming soon"**, its processor is unpublished, and it is built as a recorder with onboard storage
and BLE or Wi-Fi sync rather than as a wake-word satellite. It stays on the watchlist.
