# Audio fidelity: the 48 kHz profile

## What this is

The I2S link between the ESP32, the XMOS XVF3800 and the AIC3104 DAC runs at **48 kHz in both
directions**, matching the Seeed Studio reference profile for the reSpeaker XVF3800. Before this
it ran at 16 kHz.

## Why it was worth doing

The XMOS datasheet is specific: *16 kHz voice processing, with optional 16 kHz and 48 kHz
interface sampling rates*. So the DSP — acoustic echo cancellation, beamforming, dereverberation,
noise suppression, AGC — runs at 16 kHz **either way**. Moving the link to 48 kHz does not make
the microphone hear better, and anyone promising that is selling something.

The win is on **playback**. The Satellite Gateway's TTS arrives at 22050 Hz. On the 16 kHz link it had to be
resampled *down*, through a 7400 Hz anti-aliasing low-pass, which threw away everything between
7.4 kHz and the 11 kHz Piper actually produces. That was the "the voice sounds lower quality than
it should be" complaint, and it was the resampler doing it, not the speaker.

Measured through the new up-sampler (input amplitude 20000):

| Frequency | Through |
|---|---|
| 1 kHz | 19999 |
| 3 kHz | 19875 |
| 5 kHz | 19142 |
| 7 kHz | 17230 |
| 9 kHz | 13989 |
| 10.5 kHz | 10960 |

On the 16 kHz path every one of those above 7.4 kHz was zero.

**One honest caveat on those numbers.** They measure the *wanted* tone only. Cubic interpolation
is a compromise, not a reconstruction filter: it preserves the high band but also puts up
**spectral images** below the 24 kHz output Nyquist (a 10.5 kHz input shows an unwanted 11.55 kHz
component at comparable amplitude to the wanted one). These are imaging artifacts from the
interpolation, not aliasing — up-sampling genuinely cannot alias — but they are real extra energy
in the output. A polyphase FIR reconstruction filter would reject them and is the right answer if
that ever becomes audible; it was not done here because it is substantially more code and cubic is
already a large improvement over discarding the band outright. If you upgrade this, replace the
interpolation, do not add the 7400 Hz low-pass back.

## How the rates actually work

```
                    ┌────────────────────────────────────────────┐
   XVF3800  ──48 kHz──►  downmix      ──48 kHz──►  decimate 3:1 ──► 16 kHz ──► wake model
   (I2S RX)          │  stereo→mono               (anti-alias        │          Wyoming uplink
                    │                            FIR at 7.5 kHz)   │
                    └────────────────────────────────────────────┘

   gateway TTS ──22050 Hz──► cubic up-sample ──48 kHz──► volume ──► stereo-32 ──► ring ──► I2S TX
   local tones ──16 kHz───► cubic up-sample ──48 kHz──┘
```

Three rates, deliberately:

- **48 kHz** is the wire. It is set by `WY_AUDIO_I2S_RATE_HZ` in `wyoming_audio.h`, and
  `audio_bringup.c` / `audio_capture.c` / `audio_playback.c` each configure their channel clock
  from it. They must agree.
- **22050 Hz** is what the Satellite Gateway sends (Piper's `SND_FORMAT`).
- **16 kHz** is the wake model's input and the Wyoming mic contract. The device decimates down to
  it so nothing downstream of `audio_capture_send_chunk` had to change.

## The two conversions are NOT symmetric

This is the part worth understanding before touching the code.

**Up-sampling (22050 → 48000, and 16000 → 48000 for local tones) has no anti-aliasing filter.**
Up-sampling cannot alias. Running a low-pass here would throw away exactly the 7.4–11 kHz content
this whole profile exists to keep. The interpolation is **Catmull-Rom cubic**, not linear: linear
is a first-order hold and loses 44% of a 9 kHz tone's amplitude on this ratio, which is audible
dulling on precisely the band we are trying to preserve. Cubic costs three more multiplies per
output sample and keeps that band essentially flat.

**Down-sampling (48000 → 16000) must low-pass first.** 48 kHz input carries energy to 24 kHz, and
every component above 8 kHz folds back into the voice as distortion. `wy_decimate_48000_to_16000`
applies a 32-tap windowed-sinc FIR, Hann window, 7500 Hz cutoff (the output Nyquist is 8 kHz),
unity DC gain, and takes one sample in three. The accumulator is `int64`: 32 taps at max |tap|
9815 over full-scale `int16` input reaches ~1.0e10, which an `int32` accumulator cannot hold.

Both keep the **absolute-cursor contract**: feeding the same total input in any chunking produces
the same output sequence. `scripts/test_48k_resample.py` proves this for both, along with the
stopband, the passband, the DC gain and the absence of a time shift.

## Two traps this change already caused

**A resampler that stops when its output buffer fills marks the whole input chunk consumed
regardless.** Feed it more input than fits and the tail of the answer is silently discarded —
audible as a reply cut short, with no error anywhere. The bound is `WY_PLAYBACK_IN_MAX` (940
22050 Hz samples per call) in `wyoming_session.c`, and `wy_session_local_play` slices a tone to
`WY_PLAYBACK_MONO_MAX / 3` for the same reason. If you raise a scratch size, re-derive these.

**The `wy_session` struct shrank.** `playback_rs` changed from `wy_resampler` (a 32-tap FIR
history) to `wy_upsampler` (three samples and a ratio), so every ctypes mirror in
`scripts/test_esp32_session.py` had to change with it. The total-size assertion caught it — but
note that padding had previously absorbed a mirror gap and let a real offset error through, so
treat "the sizes match" as necessary, not sufficient.

## The hardware unknowns

**1. Does the XVF3800's firmware image follow a 48 kHz clock?** The ESP32 is the I2S clock
**master** and the XVF3800 PLLs its own MCLK from BCLK, so the rate is ours to set — but if the
image on the board is a 16 kHz-only build, driving 48 kHz will stop the mic pipeline (the symptom
noted in `audio_playback.c` when the clock was corrupted: the LED beam display goes dead and the
DAC clicks).

**2. Is the AIC3104 still inside its valid fs(ref) range?** The codec's PLL is fed from BCLK and
its rates scale with it, which is why the register sequence is unchanged. But `Register 7 D7`
declares fs(ref) as nominally 48 kHz, and tripling BCLK triples that. If playback is flaky at
48 kHz but clean at 16 kHz, this is the first suspect. (Separately: an older comment in
`audio_playback.c` claimed these registers set `J = 48`. The datasheet says the J field is
Register 4 bits D7-D2, so `0x30` is **J = 12**. The values work; the comment did not describe
them. Do not "fix" the register to match the old comment.)

### Reverting is NOT one constant — do not be misled

An earlier version of this page claimed you could set `WY_AUDIO_I2S_RATE_HZ` back to 16000 and
everything would follow. **That was wrong, and it is exactly the kind of tidy story that gets
someone stranded at the bench.** In reality:

- `audio_bringup.c`, `audio_capture.c` and `audio_playback.c` each hard-code `48000` for their own
  channel clock.
- `wy_upsampler_reset` independently assumes 22050 → 48000.
- The capture path always applies the fixed 3:1 decimator.
- `AUDIO_CAPTURE_FRAMES` is 480 to keep a 10 ms chunk at 48 kHz; at 16 kHz it must be 160.
- The codec clock registers assume a 3.072 MHz BCLK.

A revert is a **coordinated change across all of those**, not one edit. The 16 kHz down-sampler
`wy_resample_22050_to_16000` and its FIR were deliberately kept so the revert is possible at all,
but nothing makes it automatic. If a profile selector is ever wanted, it has to cover the channel
clocks, both conversions, the chunk size, the buffer sizing and the codec registers together.

## Where each piece lives

| Piece | File |
|---|---|
| The link rate | `wyoming_audio.h` → `WY_AUDIO_I2S_RATE_HZ` |
| Up-sampler (cubic) | `wyoming_audio.c` → `wy_resample_22050_to_48000`, `wy_upsampler_reset_at` |
| Decimator (FIR + 3:1) | `wyoming_audio.c` → `wy_decimate_48000_to_16000` |
| Capture decimation | `audio_capture.c` → `audio_capture_send_chunk`, `capture_decim` |
| Playback up-sample | `wyoming_session.c` → `playback_rs`, `WY_PLAYBACK_IN_MAX` |
| Local tone up-sample | `wyoming_session.c` → `wy_session_local_play` |
| Host tests | `scripts/test_48k_resample.py` |

The 16 kHz down-sampler `wy_resample_22050_to_16000` and its FIR are **kept**: they are still the
correct tool for a 16 kHz link, and if the hardware turns out not to support 48 kHz, reverting is
a constant rather than a rewrite.

## Related

- `docs/prompt-tones.md` — the tones themselves. They are rendered at 16 kHz by the same generator
  the Pi satellite uses and up-sampled on the way out, so the note timings there are unchanged.
- `docs/leds.md` — the LED phases, whose wake sweep is timed against the activation tone.
