# Prompt tones

The satellite speaks in short musical gestures, and every device in the house must use the same
ones. Someone who moves from one satellite to another should hear one voice, not two.

This document is the spec. `scripts/make_chimes.py` is the generator and carries the design notes;
the firmware generator is `wy_chime_render` / `wy_done_render` in `firmware/esp32/components/wy_wake/`.
**If you change a tone, change both, and this file.**

## The sound set

| Tone | Meaning | Notes | Length |
|---|---|---|---|
| `awake` | "I heard you" | G4 392 Hz 120 ms → 18 ms gap → C5 523 Hz 170 ms | 308 ms |
| `done` | "that answer is finished" | C5 523 Hz 120 ms → 18 ms gap → G4 392 Hz 170 ms | 308 ms |
| `connected` | "the host reached me" | E5 659 Hz 90 ms → 14 ms gap → A5 880 Hz 150 ms | 254 ms |
| `power-on` | "booted and audio is up" | G4 392 ms 140 → C5 523 140 → E5 659 200 (26 ms gaps) | 532 ms |
| `alarm` | "attention" | A4 440 Hz ×3 (170, 170, 240 ms, 70 ms gaps) | 800 ms |

`awake` and `done` are a deliberate pair: the same pitches and envelope, one ascending and one
descending. That is what makes them read as "start" and "finish" without needing a different voice.

## The envelope

Every note is a struck chime, never a flat beep:

```
n       = rate * ms / 1000
env     = exp(-t / (0.35 * ms / 1000))        # decay sized to the note's own length
attack  = first 15 ms, env *= 0.5 - 0.5*cos(pi * i / attack)   # raised cosine
sample  = sin(2*pi*freq*t) * env * 0.20
```

- **attack** 15 ms raised cosine, so there is no click on the leading edge
- **decay** exponential, with a time constant of 0.35 of the note's duration (long notes ring longer)
- **amplitude** 0.20 of full scale, roughly −14 dBFS
- **gap** 18 ms of silence between notes, so they read as two strikes rather than one

## Why these particular notes

The design notes in `scripts/make_chimes.py` are worth keeping:

> the rising 880 → 1245 Hz awake tone was "a bit jarring". 1245 Hz is high for a 1 W 8 ohm speaker
> in a small enclosure, and a flat-topped beep sounds harsher than a struck chime.

So the tones stay in roughly 390–790 Hz, use consonant intervals (a perfect fourth or fifth) rather
than a wide jump, and peak near −14 dBFS instead of −10.5. `awake` is a **rising perfect fourth**
(G4 → C5), chosen over the two brighter candidates in `sounds/candidates/`.

## Formats

| Consumer | Rate | Width | Channels |
|---|---|---|---|
| `scripts/make_chimes.py` output (`sounds/*.wav`) | 22050 Hz | 16-bit | mono |
| ESP32 firmware (`wy_*_render`) | 16000 Hz | 16-bit | mono |

Both are generated at the rate their consumer plays, so no device resamples. The pitches, timings
and envelope are identical; only the sample rate differs.

## Remaking a tone

Edit `scripts/make_chimes.py` (its `CANDIDATES`, `DONE`, `STARTUP`, `CONNECTED`, `ALARM` tables and
the `ACTIVE_AWAKE` constant), run it, and copy the resulting `sounds/*.wav` to the device's sounds
directory. The reference satellite opens the WAV per detection, so a change needs no service restart.

For the ESP32, mirror the same table in `wy_wake.c` (`wy_chime_render` and `wy_done_render`). The
regression tests read the note **frequencies back out of the rendered samples** and assert the total
length, so a re-tune that drifts from this spec fails the suite rather than shipping unnoticed:

```
.venv/bin/python -B scripts/test_esp32_wake.py
```

## Microphone mute window

The device mutes its microphone while a tone sounds, so the tone is never transcribed as speech.
Keep that margin **small**: it sits in front of the person's first word. A 308 ms tone plus a 0.4 s
margin is 708 ms of deaf microphone, which clipped the onset of real speech twice (at 908 ms and
again at 708 ms). `scripts/test_wake_mute_timing.py` asserts the bound.
