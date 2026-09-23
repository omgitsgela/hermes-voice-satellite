# Baseline device report: Seeed ReSpeaker 2-Mics Pi HAT V2.0

Captured 2026-09-23 on `wyoming1` with `scripts/device-check.py`, the current device.
Every new candidate is compared against this file. Generated output, not hand-written.

```
device check on wyoming1  (Debian GNU/Linux 12 (bookworm), kernel 6.12.109+rpt-rpi-v8, aarch64 (64bit))
  memory: {'MemTotal': '416 MB', 'MemAvailable': '185 MB'}   thermal: temp=60.7'C
throttled=0x0

  ALSA cards:
    0: 0 [vc4hdmi ]: vc4-hdmi - vc4-hdmi  <- under test

  card 'vc4hdmi        ' capture: 16 kHz mono available: False
    ALSA lib confmisc.c:165:(snd_config_get_card) Cannot get card index for vc4hdmi
    arecord: main:831: audio open error: No such device
  card 'vc4hdmi        ' playback: present

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

## Notes for comparison

- The card our stack opens is `plughw:CARD=seeed2micvoicec`; that exact string is the first thing to change for a new device.
- `spidev` present is what lets the existing APA102 LED service drive this device at all. Every USB array in `docs/potentially-supported-devices.md` uses WS2812 instead, so none of them can reuse it.
