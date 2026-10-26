# Switching the Piper voice

**Where this sits:** stage 7 speak of the voice pipeline. The end-to-end map, and every other stage, is in `docs/architecture.md`.

The satellite speaks with a Piper voice synthesized on the bridge host. Changing it is a
configuration change plus a model download; nothing in the audio contract needs to change as
long as the new voice is 22,050 Hz, which every published Piper English voice is
(`docs/audio-fidelity.md` explains why that rate is load-bearing).

This document is the whole procedure, because the parts that are not obvious — multi-speaker
voices, licensing, what to verify — are exactly where a voice change goes wrong.

## The three knobs

All live in `bridge.env` (environment variable first, then this file, then the code default in
`bridge/config.py`):

| Key | Meaning | Default |
|---|---|---|
| `VOICE_PIPER_VOICE` | Voice name, exactly as published (e.g. `en_US-libritts-high`) | `en_US-libritts-high` |
| `VOICE_PIPER_SPEAKER` | For multi-speaker voices: a speaker name from the voice's own map (`p2769`) or a numeric id. Ignored by single-speaker voices. | `p2769` |
| `VOICE_PIPER_LENGTH_SCALE` | Speaking rate: `< 1` faster, `> 1` slower. Empty uses the voice's own default. | (empty) |

The voice files live in `voices/` beside `bridge/`, named `{VOICE_PIPER_VOICE}.onnx` and
`{VOICE_PIPER_VOICE}.onnx.json`. `voices/` is **git-ignored** (not committed): each
installation downloads its own voice, and the terms of some voices do not allow redistribution
(see Licensing below). `install-gateway.sh` downloads the configured voice automatically.

## The procedure

```sh
cd ~/hermes-voice-bridge

# 1. Pick a voice. Every published name: https://huggingface.co/rhasspy/piper-voices
#    (the site's demo player is the fastest audition). Prefer a "high" quality voice for
#    adult US English.

# 2. Download it into the installation's voices/ directory.
.venv/bin/python -m piper.download_voices --download-dir voices en_US-libritts-high

# 3. If it is a MULTI-SPEAKER voice (num_speakers > 1 in its .onnx.json), choose a speaker.
#    Piper falls to an arbitrary default otherwise, and the household hears a stranger:
.venv/bin/python scripts/list-piper-speakers.py --voice en_US-libritts-high --search 2769
#    -> "p2769 -> 16"

# 4. (Optional, recommended) Listen to candidates offline before restarting anything:
#    synthesize the same sentence through each setting and compare the WAVs. The bridge's own
#    code path is the honest way to preview it:
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, '.')
from bridge import tts
from piper.config import SynthesisConfig
v = tts.load_voice()
sc = SynthesisConfig(); sc.speaker_id = 16   # p2769
import wave
with wave.open('/tmp/try-this-voice.wav', 'wb') as w:
    v.synthesize_wav("The satellite is awake. This is what the new voice sounds like.", w,
                     syn_config=sc)
PY

# 5. Point the bridge at it in bridge.env:
#      VOICE_PIPER_VOICE=en_US-libritts-high
#      VOICE_PIPER_SPEAKER=p2769
#    (VOICE_PIPER_LENGTH_SCALE only if the voice speaks too fast or too slow.)

# 6. Restart the bridge and verify the new voice is the one that loaded:
systemctl --user restart hermes-voice-bridge.service
journalctl --user -u hermes-voice-bridge -n 20 | grep -i "loading Piper voice"
#    -> "loading Piper voice en_US-libritts-high.onnx"

# 7. Say something to the satellite. Listen for: natural prosody, no robotic clipping, and
#    no lag before the first words (see Latency below).
```

To roll back: set the old values back in `bridge.env` and restart. The old voice files are
untouched; nothing else references the voice.

## Multi-speaker voices: why a speaker id is mandatory in practice

`en_US-libritts-high` carries **904 speakers**. Piper silently falls to the voice's default
speaker when none is asked for, so an unset `VOICE_PIPER_SPEAKER` means "somebody from the
LibriVox corpus," not a choice. The bridge resolves the setting through the voice's own
`speaker_id_map` and **refuses** a name that is not in it (`ValueError` in the bridge log at
the first synthesis, not a wrong voice). Prefer the name (`p2769`) over the number: published
voices have been re-released with speaker lists in a different order, so a name cannot land on
the wrong person after a re-download.

Single-speaker voices (`en_US-hfc_male-medium`, `en_US-lessac-medium`, `en_US-amy-medium`, ...)
ignore the speaker setting entirely.

There is no metadata about who the speakers are — no gender, age, or accent list. Finding one
you like is a listening exercise: `scripts/list-piper-speakers.py` prints names and ids, and
the preview snippet in step 4 renders any of them.

## Latency

Quality costs CPU. Measured on the deployment host (`i5-12500`, CPU-only, 41-word sentence):

| Voice | Synthesis speed vs realtime | First audio chunk |
|---|---|---|
| `en_US-hfc_male-medium` (the pre-2026-10-26 voice) | ~20x | < 0.1 s |
| `en_US-libritts-high` + `p2769` (the current voice) | ~4.4x | ~0.26 s |

Both stay comfortably ahead of the playback pace (`TtsStream` sends at 2x realtime) and the
first-chunk delay stays under the silence window the wake behaviour allows. But if a host is
much weaker than this, or busy running a local LLM, a `medium` voice is the fallback: the swap
is one line in `bridge.env`. Re-measure with the step-4 preview if you suspect a regression —
the voice is never the slow part of a turn (the model is), so a slow-sounding answer is not
evidence the voice is wrong.

## Licensing

Piper voices carry **their own** terms; the collection's repo license (MIT) covers the code,
not the models. Check each voice's model card at
<https://huggingface.co/rhasspy/piper-voices> before adopting it, and keep
`THIRD-PARTY.md` section C honest about the voice actually installed. The previous default,
`en_US-hfc_male-medium`, is trained on a CC BY-NC-SA dataset (non-commercial);
`en_US-libritts-high` is trained on LibriTTS (CC BY 4.0). Neither is redistributed in this
repository, which is why `voices/` is git-ignored.

## What the tests pin

`scripts/test_piper_voice_config.py` fails if:

- the voice/speaker/length knobs stop being configuration,
- a bad speaker value is silently accepted instead of refused,
- a synthesis path stops passing the resolved speaker config,
- this document stops existing or stops naming the real knob names.

`scripts/test_bridge_env.py` pins the configured defaults, so changing the installation voice
means updating that assertion in the same commit.
