# Baseline device report: Seeed ReSpeaker 2-Mics Pi HAT V2.0

Captured 2026-09-23 on the reference unit with `scripts/device-check.py`.
Every new candidate is compared against this file. The block below is generated output, not
hand-written, so re-running the script on this device should reproduce it.

```
device check on the satellite  (Debian GNU/Linux 12 (bookworm), kernel 6.12.109+rpt-rpi-v8, aarch64 (64bit))
  memory: {'MemTotal': '416 MB', 'MemAvailable': '179 MB'}   thermal: temp=61.2'C
throttled=0x0

  ALSA cards:
    0: 0 [vc4hdmi        ]: vc4-hdmi - vc4-hdmi
    1: 1 [seeed2micvoicec]: simple-card - seeed2micvoicec  <- under test

  card 'seeed2micvoicec' capture: 16 kHz mono available: False
    arecord: main:831: audio open error: Device or resource busy
    NOTE: this card is in use, so its parameters cannot be measured right now. That is expected while the voice stack is running: either stop the satellite services first, or run this check on a device before starting them.
    in use as: plughw:CARD=seeed2micvoicec,DEV=0 (from wyoming-satellite.service)
  card 'seeed2micvoicec' playback: present

  LED bus: spidev ['/dev/spidev0.0', '/dev/spidev0.1']
  buttons/input: none detected
  USB audio devices: none detected
  device overlay: dtoverlay=respeaker-2mic-v2_0
  services: {'2mic_leds': 'active', 'wake-shim': 'active', 'wyoming-openwakeword': 'active', 'wyoming-satellite': 'active'}

  What a new device will need plumbed (this is the device-profile list):
    - capture device name for the satellite's --mic-uri/--wake args (this card's id)
    - which capture channel to use (processed/beamformed channels differ per device)
    - playback device name, or an external speaker if it captures only
    - LED driver: spidev present means our APA102 service can work; none means WS2812/USB LEDs and new code
```

## What the next device has to beat or match

- Capture is `plughw:CARD=seeed2micvoicec` at 16 kHz mono. That exact card string is the first
  thing to change for a new device.
- `spidev` present is what lets the existing APA102 LED service drive a device at all. Every USB
  array in `docs/supported-devices.md` uses WS2812 instead, so none can reuse it.
- Playback comes from this HAT (speaker header). A capture-only device would leave the Pi with no
  audio output at all, because a Pi Zero 2 W has none of its own.
- No input or button device is exposed by this HAT, so any button support on a new device would be
  newly available rather than a regression.
- The capture parameters read as unmeasurable here because the running stack holds the card. For a
  real measurement, run this check on a new device BEFORE starting the services, and note that
  the in-use path is reported from the satellite unit when the card is busy.


## Re-run 2026-09-24, with the services stopped

Captured with `scripts/device-check.py` after copying the current version to the device (the copy there
was the pre-#34 version, which matters when comparing against older reports).

```
memory: 416 MB total, 229 MB available   thermal: 42.4 C, throttled=0x0
cards:  0 vc4-hdmi | 1 seeed2micvoicec (under test)
card 'seeed2micvoicec' hardware ranges: rates not reported, channels not reported
card 'seeed2micvoicec' capture: 16 kHz mono available: False   (mono ?, stereo no)
card 'seeed2micvoicec' measured capture: 16k mono ok, 16k stereo ok
card 'seeed2micvoicec' playback: present
LED bus: spidev ['/dev/spidev0.0', '/dev/spidev0.1']
overlay: dtoverlay=respeaker-2mic-v2_0
```

The advertised-parameter line and the measured line disagree, and the measured one is the truth: this card
does not report rate or channel ranges at all, and captures at 16 kHz in both mono and stereo through
`plughw`. Any future comparison should read the measured line first and treat the hardware-ranges line as
advisory only.
