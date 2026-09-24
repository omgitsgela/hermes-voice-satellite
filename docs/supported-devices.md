# Supported Devices

The units this project supports, confirmed by Angela on 2026-09-23 against the live listings.
Everything else in the ReSpeaker and Seeed families is either excluded, with the reason recorded below,
or on the watchlist pending vendor documentation.

Prices are **single-unit** prices in USD. The "10+:" figures Seeed shows on a product page are bulk tier
prices, not what a buyer pays for one.

## What counts as supported

A unit has to be one of exactly two shapes:

1. **A Raspberry Pi plus an add-on board (a HAT).** The Pi is the host, the HAT supplies the microphones
   and the control surface, and our own software drives the LEDs over SPI. This is what runs today.
2. **An integrated ESP32-class device**, with its own processor and flashable firmware. We ship our own
   firmware on these, speaking the Wyoming protocol directly to the bridge. Not ESPHome, not Linux Voice
   Assistant: those are other stacks and we are not adopting them.

**Excluded by rule: USB-only microphones and arrays.** Their audio goes over USB and their lights and
buttons are driven by the vendor's own software. Wrong shape for a device we provision, update and own.

## The supported units

| Unit | SKU | Price | Path | Role |
|---|---|---|---|---|
| **ReSpeaker 2-Mics Pi HAT V2.0** (TLV320AIC3104) | 107100001 | **$14** | A | What we run today. The reference implementation, and the baseline every other unit is compared against |
| **reSpeaker Lite Voice Assistant Kit, full kit** (2-mic array, pre-soldered XIAO ESP32S3, mono enclosed speaker, acrylic enclosure) | 110061601 | **$38** | B | **Recommended complete unit.** Only option that arrives as a finished, flashable device |
| **reSpeaker Lite Voice Assistant Kit** (array, XIAO ESP32S3, no speaker or case) | 110061601 | **$31** | B | Only worth it if a speaker and enclosure are already on hand |
| **reSpeaker XMOS XVF3800 with XIAO ESP32S3, case version** | 114993702 | **$68** | B | **Premium unit.** On-chip AEC, AGC, DoA, VAD, dereverberation, beamforming, noise suppression, 360 degree pickup to 5 m. Needs a speaker added |
| **reSpeaker XMOS XVF3800 with XIAO ESP32S3** (no case) | 114993700 | **~$67** | B | Same board, bring your own enclosure. Needs a speaker added |

- 2-Mics Pi HAT V2.0: https://www.seeedstudio.com/ReSpeaker-2-Mics-Pi-HAT.html
- Lite full kit: https://www.seeedstudio.com/ReSpeaker-Lite-Voice-Assistant-Kit-Full-Kit-of-2-Mic-Array-pre-soldered-XIAO-ESP32S3-Mono-Enclosed-Speaker-and-Enclosure.html
- Lite kit: https://www.seeedstudio.com/ReSpeaker-Lite-Voice-Assistant-Kit-p-5929.html
- XVF3800 with case: https://www.seeedstudio.com/ReSpeaker-XVF3800-With-Case-XIAO-ESP32S3-p-6628.html
- XVF3800, no case: https://www.seeedstudio.com/ReSpeaker-XVF3800-4-Mic-Array-With-XIAO-ESP32S3-p-6489.html

**Which to buy.** A complete flashable device: the **$38 full kit**. The premium unit, for hardware echo
cancellation that fixes our biggest feature gap (#1): the **$68 XVF3800 with case**. To build on a
Raspberry Pi and match what we run: the **$14 Pi HAT**.

## Excluded, and why

| Unit | Reason |
|---|---|
| reSpeaker XMOS XVF3800, bare USB (101991441, $54.90) | USB-only. Audio and controls belong to a vendor SDK |
| reSpeaker XMOS XVF3800 with case, USB (114993701, $62.99) | Same |
| reSpeaker XMOS XVF3000 with case, USB (107990193, $70.99) | Same |
| reSpeaker Flex XVF3800 Circular-4, bare USB (100005504, $57.99) | Same |
| reSpeaker Flex XVF3800 Linear-4, bare USB (100099135, $57.99) | Same |
| reSpeaker Lite, bare USB (107990273, $26.99) | Same. The Lite is only supported when paired with an ESP32 host |
| Sound Event Detection Module D1 (100049596, $18.90) | Not an audio device. An event classifier over USB serial. Its own idea, tracked separately |
| ReSpeaker 4-Mic Array for Raspberry Pi (103030216, $24.90) | Discontinued. Equivalents from other brands are unverified |

The excluded units are not bad hardware. They are good audio with someone else's control plane, and they
would leave us maintaining a vendor SDK just to change the colour of a light.

## Watchlist

**reSpeaker Clip**, wearable AI recorder: https://www.seeedstudio.com/respeaker-clip-wearable-ai-recorder.html

Fits the integrated-device shape on paper, with open-source firmware and programmable button logic and
display content. Not supported and **not to go on a buyer list**, because its firmware, SDK and datasheet
are all still marked "coming soon", its processor is unpublished, and it is built as a recorder with
onboard storage and BLE or Wi-Fi sync rather than as a wake-word satellite. Revisit when the SDK ships.

## Reference notes

**Bulk pricing.** Every "10+:" figure on a Seeed listing is a tier price. The HAT shows $11.99 against
$14 singly, and the XVF3800 case shows $62.99 against $68. Quote single-unit prices to buyers.

**Which Raspberry Pi to use.** The HAT needs a 40-pin header, and this software needs a 64-bit OS with
enough RAM to run a wake word locally. That gives three supported hosts:

- **Pi Zero 2 W** — what we run here, and the known-good build.
- **Pi 3**, all models.
- **Pi 4**, all models. The Pi 400 counts, being a Pi 4 in a keyboard.

**Do not buy a Pi Zero or Zero W, the originals.** The `seeed-voicecard` driver supports them, so they
appear on Seeed's list, but they cannot run this stack: not enough RAM for a local wake word and no
adequate 64-bit path. They are left off the list above rather than recommended-with-a-caveat, because a
caveat reads as a suggestion and this purchase would be wasted.

**The Pi 5 is not supported.** It is absent from the driver's own list, and its RP1 chip changed the audio
and GPIO plumbing. Unknown until someone tests one, so not a safe buy.

Seeed's list, for reference, is Pi 3, Pi 4, Pi Zero and Zero W on 32-bit Raspberry Pi OS with 64-bit marked
experimental. That is a driver-level list, not a statement that a board can run this project, and our
known-good build is not on it at all.

**Revision trap on the Pi HAT.** Seeed's wiki for this board mostly documents the **older V1.x built on a
WM8960 codec**. Ours is the **V2.0 with a TLV320AIC3104**, needing a different driver and overlay. The
V2.0 schematic is linked at the bottom of that wiki page.

**Full kit variant trap.** The Lite full kit comes in two variants. Only the one with the **XIAO ESP32S3
pre-soldered** has a processor. The others have nothing to flash.

**Driver coverage.** The Seeed `seeed-voicecard` driver is DKMS-installed, so it rebuilds on kernel
upgrades, and it ships device-tree overlays and mixer state for **2, 4, 6 and 8 microphone** arrays. Any
HAT matching one of those should work without new driver code. See `hardware/seeed-2mic-hat-v2.md` for the
unit in service and `device-reports/baseline-seeed-2mic-hat-v2.md` for its measured baseline.
