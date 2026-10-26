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
| `VOICE_PIPER_VOICE` | Voice name: a Piper catalog name (`en_US-lessac-medium`) or a curated external name (`clean100`) | `clean100` |
| `VOICE_PIPER_SPEAKER` | For multi-speaker voices: a speaker name from the voice's own map (`6209`) or a numeric id. Ignored by single-speaker voices. | `6209` |
| `VOICE_PIPER_LENGTH_SCALE` | Speaking rate: `< 1` faster, `> 1` slower. Empty uses the voice's own default. | (empty) |

The voice files live in `voices/` beside `bridge/`, named `{VOICE_PIPER_VOICE}.onnx` and
`{VOICE_PIPER_VOICE}.onnx.json`. `voices/` is **git-ignored** (not committed): each
installation downloads its own voice, and the terms of some voices do not allow redistribution
(see Licensing below). `install-gateway.sh` fetches the configured voice automatically:
catalog voices through `piper.download_voices`, curated external voices through
`scripts/fetch-voice.py` (below).

## The procedure

```sh
cd ~/hermes-voice-bridge

# 1. Pick a voice. Two sources:
#    a) the Piper catalog: https://huggingface.co/rhasspy/piper-voices (the site's demo
#       player is the fastest audition). Names look like en_US-lessac-medium.
#    b) curated external voices (.venv/bin/python scripts/fetch-voice.py --list), e.g.
#       clean100 — not in the catalog, hosted by its author, fetched with pinned sha256.

# 2. Download it into the installation's voices/ directory.
#    Catalog voice:
.venv/bin/python -m piper.download_voices --download-dir voices en_US-amy-high
#    Curated external voice (hash-pinned, contract-checked; piper.download_voices cannot
#    know these names):
.venv/bin/python scripts/fetch-voice.py clean100 --download-dir voices

# 3. If it is a MULTI-SPEAKER voice (num_speakers > 1 in its .onnx.json), choose a speaker.
#    Piper falls to an arbitrary default otherwise, and the household hears a stranger:
.venv/bin/python scripts/list-piper-speakers.py --voice clean100 --search 6209
#    -> "6209 -> 0"

# 4. (Optional, recommended) Listen to candidates offline before restarting anything:
#    synthesize the same sentence through each setting and compare the WAVs. The bridge's own
#    code path is the honest way to preview it:
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, '.')
from bridge import tts
from piper.config import SynthesisConfig
v = tts.load_voice()
sc = SynthesisConfig(); sc.speaker_id = 0    # speaker 6209
import wave
with wave.open('/tmp/try-this-voice.wav', 'wb') as w:
    v.synthesize_wav("The satellite is awake. This is what the new voice sounds like.", w,
                     syn_config=sc)
PY

# 5. Point the bridge at it in bridge.env:
#      VOICE_PIPER_VOICE=clean100
#      VOICE_PIPER_SPEAKER=6209
#    (VOICE_PIPER_LENGTH_SCALE only if the voice speaks too fast or too slow.)

# 6. Restart the bridge and verify the new voice is the one that loaded:
systemctl --user restart hermes-voice-bridge.service
journalctl --user -u hermes-voice-bridge -n 20 | grep -i "loading Piper voice"
#    -> "loading Piper voice clean100.onnx"

# 7. Say something to the satellite. Listen for: natural prosody, no robotic clipping, and
#    no lag before the first words (see Latency below).
```

To roll back: set the old values back in `bridge.env` and restart. The old voice files are
untouched; nothing else references the voice.

## Multi-speaker voices: why a speaker id is mandatory in practice

`clean100` carries **245 speakers** and `en_US-libritts-high` carries **904**. Piper silently
falls to the voice's default speaker when none is asked for, so an unset `VOICE_PIPER_SPEAKER`
means "somebody from the corpus", not a choice. The bridge resolves the setting through the
voice's own `speaker_id_map` and **refuses** a name that is not in it (`ValueError` in the
bridge log at the first synthesis, not a wrong voice). Prefer the name (`6209`) over the number:
published voices have been re-released with speaker lists in a different order, so a name cannot
land on the wrong person after a re-download.

clean100 names its speakers by their LibriVox reader ids (`6209`, `1088`, ...). There is no
metadata about who they are — no gender, age, or accent list — so finding one you like is a
listening exercise: `scripts/list-piper-speakers.py` prints names and ids, and the preview
snippet in step 4 renders any of them.

Single-speaker voices (`en_US-hfc_male-medium`, `en_US-lessac-medium`, `en_US-amy-medium`, ...)
ignore the speaker setting entirely.

## Latency

Quality costs CPU. Measured on the deployment host (`i5-12500`, CPU-only, 41-word sentence):

| Voice | Synthesis speed vs realtime | First audio chunk |
|---|---|---|
| `en_US-hfc_male-medium` (early voice) | ~20x | < 0.1 s |
| `en_US-libritts-high` + `p2769` (briefly the voice) | ~4.4x | ~0.26 s |
| `clean100` + speaker `6209` (the current voice) | ~19x | ~0.05 s |

clean100 is a medium-quality voice with 245 speakers: it lands back near the speed of the old
`hfc_male` while keeping multi-speaker choice, which is the best latency/quality trade found so
far on this host. For comparison, Qwen3-TTS 0.6B (a much larger neural model) measured ~0.45x
realtime on this CPU, and even Piper's `high`-quality libritts costs 4x — a 20-word answer is
audible to the device faster than it can be spoken by any of these except a GPU run.

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
`en_US-hfc_male-medium`, is trained on a CC BY-NC-SA dataset (non-commercial). The current voice,
`clean100`, is LibriTTS-r "Clean 100" (CC BY 4.0) with no further restriction from its author; its
pins and terms are recorded in `scripts/fetch-voice.py` and `THIRD-PARTY.md`. None of these is
redistributed in this repository, which is why `voices/` is git-ignored.

## What the tests pin

`scripts/test_piper_voice_config.py` fails if:

- the voice/speaker/length knobs stop being configuration,
- a bad speaker value is silently accepted instead of refused,
- a synthesis path stops passing the resolved speaker config,
- this document stops existing or stops naming the real knob names.

`scripts/test_bridge_env.py` pins the configured defaults, so changing the installation voice
means updating that assertion in the same commit.
