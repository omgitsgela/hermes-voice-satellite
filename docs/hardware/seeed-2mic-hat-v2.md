# Seeed ReSpeaker 2-Mics Pi HAT V2.0

**Status: in service, built and tested.** This is the device everything else is measured against.

## Identity, as the host sees it

| | |
|---|---|
| HAT | Seeed ReSpeaker 2-Mics Pi HAT, **V2.0 revision**, TI **TLV320AIC3104** codec |
| Device tree | `dtoverlay=respeaker-2mic-v2_0` in `/boot/firmware/config.txt` |
| ALSA card | `seeed2micvoicec` (card 1; card 0 is HDMI audio) |
| Codec driver | `tlv320aic3x`, present on I2C as `1-0018` |
| Capture | `plughw:CARD=seeed2micvoicec,DEV=0`, 16 kHz mono, S16_LE |
| Playback | same device, 22050 Hz mono S16_LE |
| LEDs | 3 x APA102 on `/dev/spidev0.1` (bus 0, device 1; the Pi also exposes `/dev/spidev0.0` on CE0, which this service does not use), driven by our own service |
| Host | Raspberry Pi Zero 2 W, 416 MB, Debian 12 arm64, kernel 6.12.109+rpt-rpi-v8 |

**Revision trap.** Most guides and product pages, including upstream's own tutorial, describe the older
**V1.x with a WM8960** codec, which is why that tutorial prints `wm8960-hifi` while this device prints
`tlv320aic3x-hifi`. Do not "fix" this device to match a guide. Confirm the revision before following any
instruction that names a codec, and note that the HAT is also sold under other brands.

## Enclosure and the way audio gets out

The reference build uses the **Voice Assistant - Wyoming Satellite** case by its designer on MakerWorld
(<https://makerworld.com/en/models/1232745-voice-assistant-wyoming-satellite>), a "Living Room Ready"
casing built for a Pi Zero 2 W with this exact HAT, with a **3070 3 W 4-ohm mono speaker**.
The listing offers five profiles, including multi-plate and single-plate AMS versions, and both **with
and without a jack cutout**.

**Which insert matches this build.** Our playback uses the HAT's **speaker header**, not its 3.5 mm jack,
so the *without jack cutout* insert is the one that matches how this device is wired. Worth confirming
against the printed part rather than assuming, since both exist in the listing.

BOM, from the designer's listing: Pi Zero 2 W with GPIO header; the ReSpeaker 2-Mics Pi HAT; a 3070
3 W 4-ohm mono speaker.

**Assembly, as the designer documents it:** connect the speaker to the connector on the HAT; position
the board with the GPIO edge under the clamp, aligning its holes with the pegs; press down gently but
firmly on the corners on the port side; slide the speaker into the rails on the front of the inner
casing; do the cable management; then slide the inner box into the outer box.

## Service arguments, exactly as deployed

From the satellite unit on the device, with the reason each one is there:

    --name 'my-satellite'
    --uri 'tcp://0.0.0.0:10700'               the only LAN port besides SSH
    --mic-command 'arecord -D plughw:CARD=seeed2micvoicec,DEV=0 -r 16000 -c 1 -f S16_LE -t raw'
    --snd-command 'aplay -D plughw:CARD=seeed2micvoicec,DEV=0 -r 22050 -c 1 -f S16_LE -t raw'
    --wake-uri 'tcp://127.0.0.1:10400'        the wake shim, not the wake engine directly
    --wake-word-name '<your wake word>'
    --event-uri 'tcp://127.0.0.1:10500'       how the bridge drives the LEDs
    --awake-wav  ~/sounds/awake.wav
    --done-wav   ~/sounds/done.wav
    --mic-seconds-to-mute-after-awake-wav 0.1
    --tts-played-command '/usr/bin/python3 .../examples/led_cmd.py idle'

The unit also has an `ExecStartPre` that plays `power-on.wav` exactly once per boot, gated by a `/run`
marker, so a service restart or a crash loop cannot turn it into an alarm. The done tone is muted across
the microphone by our core patch, because otherwise the device hears its own chime and answers it.

## Tests to run on a rebuilt unit

1. `scripts/device-check.py` on the device, **before** starting the services, and keep the output.
2. `arecord -l` must show `seeed2micvoicec`; if it does not, the driver or the overlay is missing.
3. Wake word fires from the far side of the room, and the answer is audible through the speaker.
4. The startup tone plays once per boot, and stays silent across a manual restart.
5. The LEDs pulse on boot, go green once on the first connection, and show red only after a real outage.
6. `journalctl -u wyoming-satellite | grep -c underrun` after an hour of normal use.

## Known quirks and open items

- **The driver is DKMS**, so a kernel upgrade rebuilds it. If the card ever disappears after an upgrade,
  the build failed: `/usr/sbin/dkms status` and `dkms install` are the first two commands.
- The HAT's **3.5 mm jack is unused**; playback goes through the speaker header. If a future build uses
  the jack, the *with jack cutout* insert is the matching print.
- **Timers are available on the satellite and not wired up.** Our pinned revision supports
  `--timer-finished-wav`, `--timer-finished-wav-repeat` and the timer event commands. Issue #5 assumed
  the bridge would own the whole timing path; it may only need to drive the satellite's own timer.
- **`--mic-auto-gain`, `--mic-noise-suppression` and `--snd-volume-multiplier` are all unused**, and all
  three are plausible wins: the first two against the long endpointer waits in a noisy room, the third
  for voice volume control in #10.
- The obsolete `pi/wyoming-satellite-vad.conf` drop-in was removed in issue #38. It falsely claimed
  that VAD enabled wake-free streaming and would have restored the retired `ok_nabu` wake word if
  installed. Follow-up capture uses the wake shim; do not recreate or deploy a VAD override.
