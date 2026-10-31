# Supported Devices

**Where this sits:** stage 1 capture of the voice pipeline. The end-to-end map, and every other stage, is in `docs/architecture.md`.

The units this project supports, confirmed against the live listings on 2026-09-23.
Everything else in the ReSpeaker and Seeed families is either excluded, with the reason recorded below,
or on the watchlist pending vendor documentation.

Prices are **single-unit** prices in USD. The "10+:" figures Seeed shows on a product page are bulk tier
prices, not what a buyer pays for one.

## What counts as supported

A unit has to be one of exactly two shapes:

1. **A Raspberry Pi plus an add-on board (a HAT).** The Pi is the host, the HAT supplies the microphones
   and the control surface, and our own software drives the LEDs over SPI. This is what runs today.
2. **An integrated ESP32-class device**, with its own processor and flashable firmware. We ship our own
   firmware on these, speaking the Wyoming protocol directly to the Satellite Gateway. Not ESPHome, not Linux Voice
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

**reSpeaker Clip**, wearable AI recorder, SKU 100020126, $75.90 in stock at the Seeed US warehouse
(checked 2026-09-24): https://www.seeedstudio.com/respeaker-clip-wearable-ai-recorder.html

**Re-checked 2026-09-24 and rejected on the evidence, not on availability.** An earlier version of this
entry said the Clip's firmware, SDK and datasheet were all "coming soon" and its processor unpublished.
Two of those three were wrong. The processor is a **nRF5340 + nRF7002** pair, published in the wiki spec
table and tagged on the product page, and the firmware SDK has been public since January. It is a
**public Apache-2.0 Zephyr repository** (`github.com/Seeed-Studio/reSpeaker_Clip`) with the AT protocol,
UDP transfer and USB DFU documented, Python and mobile SDKs, and **downloadable signed firmware on its
GitHub Releases** (v0.1.4, published 2026-09-21). Only the raw datasheet, the mechanical drawing and
Seeed's own wiki download row still lag.

It stays off the buyer list, and the reason is not availability: it is a recorder rather than a satellite.
The vendor's own documentation is the evidence, and the full list follows.

**Why it will not work here, in order of how fatal each one is.** Every claim below is from the vendor's own
firmware documentation, checked 2026-09-24.

1. **There is no live audio path, and the vendor says so.** The firmware guide lists BLE real-time audio
   streaming as out of scope: "the BLE link carries AT commands, file transfer frames, and an audio
   energy-level visualization notify only. **It does not stream live audio.**" Audio goes PDM microphones
   into preprocessing into an Opus encoder at 32 kbps mono and lands in storage. Our gateway needs a
   continuous 16 kHz mono PCM stream arriving on a socket, and the Clip has no mode that produces one. A
   file-transfer protocol is not a substitute: it moves finished recordings, not the microphone.
2. **It is not a Pi peripheral either, so the HAT path is closed too.** USB on this device is CDC ACM
   serial and mass storage. There is no USB Audio Class, so no ALSA card appears for
   `wyoming-satellite` to capture from, and the charging base's USB-C is power to the base rather than an
   audio datapath.
3. **It cannot speak.** The box contains the Clip, a lapel clip, a magnetic patch, a charging base, a cable
   and a lanyard. No speaker, and "audio output" in the specification means the stored file. A satellite
   that cannot answer, chime or make an announcement is half a device, and this half is the half we need
   most.
4. **There is no wake word and no always-on listening.** It records when the button tells it to and stops
   when told. The firmware's capability table has no wake-word engine, and idle current is quoted around
   170 microamps, which is a device that sleeps rather than one that listens.
5. **The intelligence sits off the device.** The same guide states plainly that there is no on-device
   speech-to-text path, that the Clip writes Opus to storage and that transcription runs elsewhere, in
   Seeed's app or their cloud. So it cannot host the wake or the speech stage of our stack either.
6. **What turning it into a satellite would actually take.** A Wyoming client written in Zephyr against
   Nordic's SDK, a real-time audio mode that its own firmware declares out of scope, and a speaker with
   amplification added to a sealed enclosure with no accessible debug header. That is a project aimed at
   the wrong shape, for more money than a complete, flashable, speaker-included kit.

It is a reasonable product for what it is: a wearable meeting and interview recorder with local storage and
a hosted transcription app. That is a separate purchase decision, and not a component of this system.

## Reference notes

**Bulk pricing.** Every "10+:" figure on a Seeed listing is a tier price. The HAT shows $11.99 against
$14 singly, and the XVF3800 case shows $62.99 against $68. Quote single-unit prices to buyers.

**Which Raspberry Pi to use.** The HAT needs a 40-pin header, and this software needs a 64-bit OS with
enough RAM to run a wake word locally. That gives three supported hosts:

- **Pi Zero 2 W** — what we run here, and the known-good build.
- **Pi 3**, all models.
- **Pi 4**, all models. The Pi 400 counts, being a Pi 4 in a keyboard.

**Buying a Zero 2 W** (prices and stock checked 2026-09-24). The HAT is a push-on board, so the host needs
a 40-pin header fitted before the HAT will seat. That is the trap with the bare board: it ships without one.

- **Raspberry Pi Zero 2 W with Header**, SKU 102111003, $18.00, 40-pin header pre-soldered. **Out of stock**
  on the day checked, with a notify option on the listing. The tidy option when it is available.
  https://www.seeedstudio.com/Raspberry-Pi-Zero-2-W-with-Header-p-5935.html
- **Raspberry Pi Zero 2 W Basic Kit**, SKU 114060101, $19.90, in stock. Board plus a mini-HDMI adapter, a
  USB OTG adapter, a heatsink and a **loose 40-pin header**, so the header has to be soldered on before the
  HAT fits. Worth the extra dollar only if the adapters are wanted.
  https://www.seeedstudio.com/Raspberry-Pi-Zero-2-W-Basic-Kit-p-6352.html

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

**Verify a device against the vendor's repository, not the wiki's Resources table.** The Clip entry above
was wrong for months because a "Coming soon" row on Seeed's wiki was read as the state of the SDK while the
firmware source, its protocol documentation and signed release binaries were already public on GitHub. A
vendor wiki is a convenience index and lags; the repository and its releases are the artifact. Check the
repo, the releases page and the product page's own spec tags before writing an availability claim into this
document, and date the claim when it is price or stock.
