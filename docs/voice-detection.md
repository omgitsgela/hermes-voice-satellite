# Voice detection: which detector decides the device hears a person

**Where this sits:** stage 4 endpoint of the voice pipeline. The end-to-end map, and every other stage, is in `docs/architecture.md`.

The endpointer turns a continuous microphone stream into one trimmed utterance per turn. It asks two
questions about every 20 ms frame, and they are different questions:

1. **Does this sound like a voice?** - the voice activity detector.
2. **Is it loud enough to be a person in this room?** - the energy gate.

This document is about the first. The second is in the comment above `VOICE_ENERGY_GATE` in
`gateway/config.py`, and its bug is in `scripts/test_energy_gate.py`.

## The change, and why it was forced

Until 2026-09-25 the detector was `webrtcvad` at aggressiveness 2. The report that forced the change: the satellite
recognising its wake word and then answering nothing, in a busy computer centre office with a fan and
other people. The Gateway log said `nothing said after the wake word` nine times running, each turn
carrying `0.52s audio, 0.02s speech` - about 2% of a five-second window.

The instrumented gate showed `vad_speech=46 gate_rejected=26`: the detector was finding her, and the
**gate** was throwing most of it away. Its threshold had climbed to ~3337, because the office's noise
floor learned at ~540 against 142-190 at home, and the threshold is the floor times six. Her speech
sat at ~1040-1723. **The gate was set above the person.**

That is fixed in the gate (a ceiling - see `test_energy_gate.py`). But the deeper finding came from
comparing the two detectors on identical audio:

| signal | `webrtcvad` | Silero |
|---|---|---|
| real speech (the 5 s reference recording) | 47.6% | 47.6% |
| broadband noise, quiet room (rms ~350) | 2.7% | **0.0%** |
| broadband noise, busy room (rms ~1170) | 3.3% | **0.0%** |
| broadband noise, loud room (rms ~3500) | **100.0%** | **0.0%** |
| 120 Hz rumble, fan-like | - | **0.0%** |
| silence | - | **0.0%** |

`webrtcvad` tolerates a quiet room and then **calls a loud one entirely speech**. Its answer moves
with the room rather than with the voice, which is the wrong dependence for a device meant to work in
"all kinds of places". Silero answers zero at every level measured.

So the energy gate is not a bad idea badly executed - it is a patch over a detector that could not
tell a fan from a person. Silero removes the reason the patch had to be aggressive.

## What is used, and where it lives

- `gateway/silero_vad.py` - the detector, a thin wrapper mirroring `webrtcvad.Vad.is_speech`.
- `gateway/vendor/silero_vad_v6.onnx` - the model, **committed**, with its checksum in
  `gateway/vendor/MANIFEST.md`.
- `VOICE_DETECTOR` in `gateway/config.py` - `silero` (default) or `webrtcvad`.
- `scripts/test_silero_vad.py` - the comparison above, as assertions.

Nothing is downloaded at runtime. The upstream project is tracked as a pinned git bundle by
`scripts/refresh-upstream-mirrors.py` (`silero-vad`), so the model can be regenerated without
network.

`webrtcvad` is deliberately still reachable. Every measurement this project took before this date was
taken with it, and being able to switch detectors by configuration turns a comparison into a config
change instead of an argument.

## The streaming detail that will bite

The endpointer feeds 20 ms frames (320 samples at 16 kHz). **Silero works in 512-sample windows with
64 samples of context carried between them** - 576 samples per inference.

So `SileroVad` buffers frames into windows, and a call that does not complete a window returns the
most recent decision rather than raising, because the endpointer has no idea where window boundaries
fall. Two consequences worth knowing before reading a log:

- The decision lags the audio by up to one window (32 ms). Irrelevant for endpointing, but it means
  the detector cannot be used to timestamp anything precisely.
- Feeding a large block at once runs every completed window rather than skipping the extras. Dropping
  them would silently discard audio.

The ONNX model also carries LSTM state (`h`, `c`) across windows, so `reset()` must be called between
utterances or one turn's audio colours the next.

## Two measurement traps, recorded because both produced confident wrong answers

**1. `webrtcvad.Vad` carries state between calls.** An early comparison reused one instance across
several signals and reported "99% of room noise is called speech". Feeding each signal to a **fresh**
instance gives 2.7% at that same level. The 99% figure was the previous signal leaking into the next
verdict. Any future comparison must build a new detector per signal - `scripts/test_silero_vad.py`
does, and says why in a comment.

**2. A scale factor computed from a remembered level rather than the measured one.** Scaling noise by
"350/350" instead of by its actual RMS multiplied by 3500 and overflowed int16. Measure the base
level; do not assume it.

## The wake-window level (cross-device arbitration, #32)

When several armed devices hear the *same* utterance, the conversation goes to the device that
heard it best. A device cannot know how loudly the others heard it, so each device measures its
own microphone loudness and reports it on the detection; the Gateway compares the reports inside
one claim window. The measurement lives in the wake core (`wy_wake.c`), host-tested in
`scripts/test_esp32_wake.py` and proven on the wire in `scripts/test_esp32_wake_integration.py`:

- an exponentially smoothed mean-square of the **armed, actually scored** frames (~100 ms
  effective window at the 10 ms frame cadence — a spoken wake phrase brackets well inside it);
- frozen **at fire** together with the crossing score, so the numbers describe the detection,
  not the live room;
- frames consumed while deaf (streaming, playback, post-stream cooldown) never contribute — the
  level describes the room, never the device's own speaker or the tail of an answer;
- the reported scale is fixed and installation-independent: `20*log10(rms/32768)` mapped over
  [-60 dBFS, 0 dBFS] to 0..100. **`-1` means "no measurement" and is distinct from exact
  silence (0)**: a silent device ranks below a hearing one, while an unprovisioned or
  model-less core can never win arbitration with a stale or fake number;
- levels compare **only across devices for one utterance inside the claim window** — never
  across time, never across installations with different mic gains. They are an ordering
  signal, not an absolute loudness.

The Gateway side (`gateway/wake_router.py`): level dominates, wake score is the tiebreaker,
arrival order is the last resort; a measured level always beats an unmeasured one, and two
unmeasured detections behave exactly like the old first-arrival rule. The claim window is
`VOICE_WAKE_CLAIM_WINDOW_MS` (0 = first arrival wins, which is also what a fleet of older
firmware gets automatically).

A Pi satellite with a wake-word service behind a wake shim (`pi/wake_shim.py`) gets the same
arbitration without touching the wake engine: the shim already relays the satellite's microphone
audio byte for byte to the wake service, so it meters the same bytes with a fixed window of the
last 1600 samples and, when it relays a detection back down, attaches `context.wake_level` on the
identical scale. The meter is a passenger: it can never alter, hold or reorder the relayed audio,
and if it loses framing it switches itself off with one log line rather than guess. An injected
wake-now detection has no wake audio behind it, so it stays the historical, context-free shape.
Host-tested in `pi/test_wake_shim.py`.

## Verifying a change here

```
.venv/bin/python scripts/test_silero_vad.py     # the detector itself
.venv/bin/python scripts/test_energy_gate.py    # the gate, including the busy-office case
.venv/bin/python scripts/test_endpoint_diagnostics.py   # the per-turn levels: line
```

To hear the difference on real audio, set `VOICE_DETECTOR = "webrtcvad"`, restart
`hermes-voice-gateway.service`, and compare the `vad_speech` figure in the `levels:` log line against
the same utterance with `silero`. The gate and the detector are instrumented separately precisely so
that a failure can be attributed to one of them rather than guessed at.
