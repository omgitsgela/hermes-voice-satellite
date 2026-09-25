#!/usr/bin/env python3
"""Generate the satellite's prompt tones, plus candidate awake tones to choose from.

Angela's feedback on v1: the rising 880 -> 1245 Hz awake tone was "a bit jarring". 1245 Hz
is high for a 1 W 8 ohm speaker in a small enclosure, and a flat-topped beep sounds harsher
than a struck chime. So v2:

  * stays in the roughly 390-790 Hz band, where a tiny speaker is still comfortable
  * uses consonant intervals (a perfect fourth or fifth) instead of a wide jump
  * gives every note a 15 ms attack and an exponential decay, so it reads as a chime
  * peaks near -14 dBFS rather than -10.5

`ACTIVE_AWAKE` is the design actually written to sounds/awake.wav. Listen to the candidates
with `preview_awake_candidates.py`, then change that constant, re-run this, and copy to the
Pi. The satellite opens the WAV per detection (satellite.py:646-669), so changing a tone
needs no service restart.

Wiring (the satellite's own flags, not the bridge's):
    --awake-wav /home/wyoming1/sounds/awake.wav
    --done-wav  /home/wyoming1/sounds/done.wav
    --mic-seconds-to-mute-after-awake-wav 0.6

The satellite mutes the microphone for the awake wav's duration PLUS that setting
(satellite.py:656-662), so the tone is never captured into the utterance that follows.

Generated at exactly the satellite's snd format (22050 Hz, 16-bit, mono) so the Pi does no
resampling. Written locally rather than downloaded: it must be free to use on our hardware.
"""

import wave
from pathlib import Path

import numpy as np

RATE = 22050
WIDTH = 2
CHANNELS = 1
OUT = Path(__file__).resolve().parent.parent / "sounds"

# Two-note rising designs, all deliberately low. (frequency Hz, duration ms)
CANDIDATES = {
    # A4 -> E5, a rising perfect fifth: the classic "attention" interval, warm and low.
    "a-warm-fifth": [(440, 120), (660, 170)],
    # C5 -> G5, the same interval one step brighter.
    "b-bright-fifth": [(523, 110), (784, 160)],
    # G4 -> C5, a rising fourth: the softest and lowest of the three.
    "c-low-fourth": [(392, 120), (523, 170)],
}
ACTIVE_AWAKE = "c-low-fourth"

# done: the SAME two pitches as the chosen awake tone, reversed (C5 -> G4, a falling fourth).
# Same register, same interval, same level, so the pair sounds of a piece, while still
# reading as "finished" because it descends.
DONE = [(523, 120), (392, 170)]

# Startup feedback, added 2026-09-22 at Angela's request. Same envelope, register and level as
# everything else, distinguished by note count and rhythm rather than by volume:
#   power-on  : three slow rising notes, "the Pi has booted and audio is up"
#   connected : two quick bright notes, "the host has dialled in, I am actually functional"
# Neither is the awake or done tone, so neither can be mistaken for the device having heard
# something.
STARTUP = [(392, 140), (523, 140), (659, 200)]     # G4 -> C5 -> E5
CONNECTED = [(659, 90), (880, 150)]                # E5 -> A5, snappier and brighter
# Alarm: three deliberate repeated notes, recognisably different from the timer's
# vendored descending two-note sound and from the conversational awake/done pair.
ALARM = [(440, 170), (440, 170), (440, 240)]


def chime(freq: float, ms: float, *, amplitude: float = 0.20,
          attack_ms: float = 15.0, decay: float = 0.35) -> np.ndarray:
    """One struck-note-like tone: soft attack, exponential decay, no flat top."""
    n = int(RATE * ms / 1000)
    t = np.arange(n) / RATE
    env = np.exp(-t / max(decay * ms / 1000, 1e-6))
    attack = min(int(RATE * attack_ms / 1000), n)
    if attack:
        env[:attack] *= 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, attack))
    return np.sin(2 * np.pi * freq * t) * env * amplitude


def silence(ms: float) -> np.ndarray:
    return np.zeros(int(RATE * ms / 1000))


def build(notes, *, gap_ms: float = 18.0) -> np.ndarray:
    """Concatenate notes, with a small gap so they read as two distinct notes."""
    parts = []
    for index, (freq, ms) in enumerate(notes):
        if index:
            parts.append(silence(gap_ms))
        parts.append(chime(freq, ms))
    return np.concatenate(parts)


def write_wav(path: Path, samples: np.ndarray) -> dict:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(WIDTH)
        wf.setframerate(RATE)
        wf.writeframes(pcm.tobytes())
    peak = int(np.abs(pcm).max())
    return {
        "ms": len(pcm) / RATE * 1000,
        "peak": peak,
        "dbfs": 20 * np.log10(peak / 32768) if peak else float("-inf"),
    }


print(f"=== PROMPT TONES v2: {RATE} Hz, {WIDTH * 8}-bit, mono, chime envelope ===")

print("\ncandidates for the awake tone:")
for name, notes in CANDIDATES.items():
    info = write_wav(OUT / "candidates" / f"awake-{name}.wav", build(notes))
    hz = " -> ".join(f"{f}" for f, _ in notes)
    mark = "   <== ACTIVE" if name == ACTIVE_AWAKE else ""
    print(f"  {name:16s} {hz:12s} {info['ms']:5.0f} ms  {info['dbfs']:5.1f} dBFS{mark}")

print(f"\ninstalled (ACTIVE_AWAKE = {ACTIVE_AWAKE!r}):")
info = write_wav(OUT / "awake.wav", build(CANDIDATES[ACTIVE_AWAKE]))
print(f"  awake.wav  {info['ms']:.0f} ms  {info['dbfs']:.1f} dBFS")
info = write_wav(OUT / "done.wav", build(DONE))
print(f"  done.wav   {info['ms']:.0f} ms  {info['dbfs']:.1f} dBFS")

print("\nstartup feedback:")
info = write_wav(OUT / "power-on.wav", build(STARTUP, gap_ms=26))
print(f"  power-on.wav   {info['ms']:.0f} ms  {info['dbfs']:.1f} dBFS  (three rising notes, "
      f"played by the satellite on the Pi)")
info = write_wav(OUT / "connected.wav", build(CONNECTED, gap_ms=14))
print(f"  connected.wav  {info['ms']:.0f} ms  {info['dbfs']:.1f} dBFS  (two quick bright notes, "
      f"played by the bridge on connect)")
info = write_wav(OUT / "alarm.wav", build(ALARM, gap_ms=70))
print(f"  alarm.wav      {info['ms']:.0f} ms  {info['dbfs']:.1f} dBFS  (three repeated notes, "
      f"used only by cron-backed alarms)")

print(f"\nwritten to {OUT}")
print("Preview:  .venv/bin/python scripts/preview_awake_candidates.py")
print("Install:  scp sounds/awake.wav wyoming1:/home/wyoming1/sounds/   (no restart needed)")
