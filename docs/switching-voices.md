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
| `VOICE_PIPER_VOICE` | Voice name: a Piper catalog name (`en_US-hfc_male-medium`) or a curated external name (`clean100`) | `en_US-hfc_male-medium` |
| `VOICE_PIPER_SPEAKER` | For multi-speaker voices: a speaker name from the voice's own map (`78`) or a numeric id. Ignored by single-speaker voices. | (empty — the default voice has one speaker) |
| `VOICE_PIPER_LENGTH_SCALE` | Speaking rate: `< 1` faster, `> 1` slower. Empty uses the voice's own default. | (empty) |

The voice files live in `voices/` beside `bridge/`, named `{VOICE_PIPER_VOICE}.onnx` and
`{VOICE_PIPER_VOICE}.onnx.json`. `voices/` is **git-ignored** (not committed): each
installation downloads its own voice, and the terms of some voices do not allow redistribution
(see Licensing below). `install-gateway.sh` fetches the configured voice automatically:
catalog voices through `piper.download_voices`, curated external voices through
`scripts/fetch-voice.py` (below).

## The one command

```sh
scripts/switch-voice.py --list                            # what is installed, and what is live
scripts/switch-voice.py en_US-lessac-medium               # switch both surfaces
scripts/switch-voice.py clean100 --speaker 78             # multi-speaker: bridge gets the NAME, Hermes the id
scripts/switch-voice.py en_US-amy-medium --scope gateway  # chat only, leave the satellite alone
scripts/switch-voice.py --rollback                        # undo the last switch
scripts/switch-voice.py <voice> --dry-run                 # show the plan, change nothing
```

It does the four things that are easy to get wrong by hand, in one pass:

1. **fetches the voice** if it is not installed (the curated list first via
   `scripts/fetch-voice.py`, then the Piper catalog) and **refuses anything that is not
   22,050 Hz** before it writes a single file.
2. **installs it twice** — `voices/` for the bridge and `~/.hermes/cache/piper-voices/` for
   Hermes, which never downloads a name it does not recognise and otherwise fails quietly at
   synthesis time rather than at switch time.
3. **writes the two shapes of the same setting**: `bridge.env` gets the speaker NAME from the
   voice's own map, Hermes gets the numeric ID that name resolves to, and a single-speaker voice
   has `speaker_id` *removed* rather than set to 0.
4. **backs up both files, restarts the bridge, then verifies** that the service is active and the
   running process really carries the new `VOICE_PIPER_VOICE`, recording what it did in
   `state/voice/state.json` so `--rollback` needs no notes and no `/tmp`.

## A preference change is not a code change

Two separate decisions, deliberately kept apart:

| Decision | Where it lives | How to change it |
|---|---|---|
| Which voice **this installation** speaks with | `bridge.env` + Hermes `config.yaml` (both git-ignored) | `scripts/switch-voice.py <voice>` |
| Which voice a **fresh clone** defaults to | `PIPER_VOICE_NAME` in `bridge/config.py`, mirrored in `bridge.env.example` | a code change, with docs |

Changing what you listen to should never need a pull request, a test update, or a re-read of this
document. The tests pin the *contract* — a plausible catalog-style default that agrees with
`bridge.env.example` — rather than one particular favourite voice, so a preference change cannot
break CI.

## Doing it by hand (what the command automates)

The rest of this section is the manual equivalent. It is kept because it documents exactly which
files a switch touches, and because some installations cannot run the command (no systemd, or a
bridge on a different host from the Hermes gateway).

```sh
cd ~/hermes-voice-bridge

# 1. Pick a voice. Two sources:
#    a) the Piper catalog: https://huggingface.co/rhasspy/piper-voices (the site's demo
#       player is the fastest audition). Names look like en_US-lessac-medium.
#    b) curated external voices (.venv/bin/python scripts/fetch-voice.py --list), e.g.
#       clean100 — not in the catalog, hosted by its author, fetched with pinned sha256.
#
#    Voices already installed in voices/ on this host (all auditioned 2026-10-26, all 22,050 Hz,
#    all kept for rollback and future auditions): en_US-hfc_male-medium (the current voice),
#    clean100 (245 speakers; listened to 0/6209 and 36/78), en_US-libritts-high (904 speakers;
#    listened to p2769).

# 2. Download it into the installation's voices/ directory.
#    Catalog voice:
.venv/bin/python -m piper.download_voices --download-dir voices en_US-amy-high
#    Curated external voice (hash-pinned, contract-checked; piper.download_voices cannot
#    know these names):
.venv/bin/python scripts/fetch-voice.py clean100 --download-dir voices

# 3. If it is a MULTI-SPEAKER voice (num_speakers > 1 in its .onnx.json), choose a speaker.
#    Piper falls to an arbitrary default otherwise, and the household hears a stranger:
.venv/bin/python scripts/list-piper-speakers.py --voice clean100 --search 78
#    -> "78 -> 36" (the substring search also shows 7800, 6078, ...; use the exact name)
# 4. (Optional, recommended) Listen to candidates offline before restarting anything:
#    synthesize the same sentence through each setting and compare the WAVs. The bridge's own
#    code path is the honest way to preview it:
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, '.')
from bridge import tts
from piper.config import SynthesisConfig
v = tts.load_voice()
sc = SynthesisConfig(); sc.speaker_id = 36   # speaker 78
import wave
with wave.open('/tmp/try-this-voice.wav', 'wb') as w:
    v.synthesize_wav("The satellite is awake. This is what the new voice sounds like.", w,
                     syn_config=sc)
PY

# 5. Point the bridge at it in bridge.env:
#      VOICE_PIPER_VOICE=clean100
#      VOICE_PIPER_SPEAKER=78
#    (a catalog single-speaker voice needs only VOICE_PIPER_VOICE; leave the speaker empty)
#    (VOICE_PIPER_LENGTH_SCALE only if the voice speaks too fast or too slow.)

# 6. Restart the bridge and verify the new voice is the one that loaded:
systemctl --user restart hermes-voice-bridge.service
journalctl --user -u hermes-voice-bridge -n 20 | grep -i "loading Piper voice"
#    -> "loading Piper voice clean100.onnx"

# 7. Say something to the satellite. Listen for: natural prosody, no robotic clipping, and
#    no lag before the first words (see Latency below).
```

To roll back by hand: set the old values back in `bridge.env` and restart. The old voice files are
untouched; nothing else references the voice. `scripts/switch-voice.py --rollback` does the same
thing from the recorded state, including the Hermes side.

## Voices auditioned on this installation

Measured on an i5-12500 CPU-only host, same sentence, median F0 over voiced frames. Worth reading
before spending an evening re-auditioning: pitch and speed do not drift between sessions.

| Voice | Speakers | F0 | Speed | Licence | Verdict |
|---|---|---|---|---|---|
| `en_US-hfc_male-medium` | 1 | ~120 Hz | ~19-20x realtime | dataset CC BY-NC-SA | the shipped default; kept |
| `en_US-libritts-high` | 904 | `p2769` 109 Hz | ~4.4x | LibriTTS, CC BY 4.0 | rejected on sound; still installed |
| `clean100` (external) | 245 | sp0 118 Hz, sp36 (`78`) 100 Hz | ~20-25x | CC BY 4.0 | rejected on sound; still installed |
| `en_US-OptimusPrime-medium` (external, unofficial) | 1 | ~92 Hz | ~21-29x | CC BY-NC 4.0 plus rights caveats | fun; installation-local only |
| Qwen3-TTS 0.6B GGUF (not a Piper voice) | 9 baked | — | ~0.45x | Apache-2.0 | far too slow for the satellite loop |

An audition can legitimately end in "none of these" — that is why the switch is a one-liner now.

## Keeping the Hermes platform voice in step

The satellite and the chat platform deliberately speak with the same voice. Hermes keeps its own
voice cache (`~/.hermes/cache/piper-voices/`) and its own keys:

```sh
hermes config set tts.piper.voice <name>
hermes config set tts.piper.speaker_id <numeric id>   # Hermes wants the NUMBER, the bridge the NAME
```

For a **catalog** voice that is the whole story: Hermes downloads it on first use. For a
**curated external** voice like `clean100`, Hermes' downloader does not know the name either, so
seed its cache with the same audited fetcher before the first synthesis (an unseeded cache means
every TTS call logs a failed download and nothing speaks):

```sh
.venv/bin/python scripts/fetch-voice.py clean100 --download-dir ~/.hermes/cache/piper-voices
```

Hermes resolves a cache hit before it ever tries to download, so a seeded voice loads exactly like
a catalog one. The live pair right now: `en_US-hfc_male-medium` with `VOICE_PIPER_SPEAKER` unset
on the bridge, and `tts.piper.voice: en_US-hfc_male-medium` on Hermes with no `speaker_id` (a
single-speaker voice needs none). The clean100 pair — `VOICE_PIPER_SPEAKER=78` and
`tts.piper.speaker_id: 36` — is what that looked like while clean100 was installed, and is the
example to follow for any multi-speaker voice.

## Multi-speaker voices: why a speaker id is mandatory in practice

`clean100` carries **245 speakers** and `en_US-libritts-high` carries **904**. Piper silently
falls to the voice's default speaker when none is asked for, so an unset `VOICE_PIPER_SPEAKER`
means "somebody from the corpus", not a choice. The bridge resolves the setting through the
voice's own `speaker_id_map` and **refuses** a name that is not in it (`ValueError` in the
bridge log at the first synthesis, not a wrong voice). Prefer the name (`78`) over the number:
published voices have been re-released with speaker lists in a different order, so a name cannot
land on the wrong person after a re-download.

clean100 names its speakers by their LibriVox reader ids (`78`, `1088`, ...), not by position.
That is a trap worth knowing: the author's own sample zip numbers files by **numeric id**
(`0.wav` ... `244.wav`), so "voice 36" on his page means numeric id 36, which in this voice's
map is the name `78`. Configure the name, not the number, and `scripts/list-piper-speakers.py`
is the lookup that turns one into the other. There is no metadata about who the speakers are —
no gender, age, or accent list — so finding one you like is a listening exercise, and the preview
snippet in step 4 renders any of them.

Single-speaker voices (`en_US-hfc_male-medium`, `en_US-lessac-medium`, `en_US-amy-medium`, ...)
ignore the speaker setting entirely.

## Latency

Quality costs CPU. Measured on the deployment host (`i5-12500`, CPU-only, 41-word sentence):

| Voice | Synthesis speed vs realtime | First audio chunk |
|---|---|---|
| `en_US-hfc_male-medium` (the current voice) | ~20x | < 0.1 s |
| `en_US-libritts-high` + `p2769` (auditioned 2026-10-26) | ~4.4x | ~0.26 s |
| `clean100` + speaker `78` (auditioned 2026-10-26) | ~20x | ~0.05 s |

clean100 is a medium-quality voice with 245 speakers: it lands back near the speed of
`hfc_male` while keeping multi-speaker choice. For comparison, Qwen3-TTS 0.6B (a much larger
neural model) measured ~0.45x
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
`THIRD-PARTY.md` section C honest about the voice actually installed. The default,
`en_US-hfc_male-medium`, is trained on a **CC BY-NC-SA** dataset (non-commercial) — fine for this
household, worth knowing before publishing anything spoken by it. Voices with more permissive
terms exist and were auditioned on 2026-10-26: the current lineup did not win on sound, but the
audition samples and their measurements are in the operations journal. `clean100` (LibriTTS-r
"Clean 100", **CC BY 4.0**, no further restriction from its author) is the permissive option kept
installed; its pins and terms are recorded in `scripts/fetch-voice.py` and `THIRD-PARTY.md`. None
of these is redistributed in this repository, which is why `voices/` is git-ignored.

## What the tests pin

`scripts/test_piper_voice_config.py` fails if:

- the voice/speaker/length knobs stop being configuration,
- a bad speaker value is silently accepted instead of refused,
- a synthesis path stops passing the resolved speaker config,
- this document stops existing or stops naming the real knob names.

`scripts/test_bridge_env.py` pins that the voice keys are populated from the defaults; it no longer
pins a specific voice, so an installation (or a future maintainer) can change the shipped default
without collateral test edits — but `test_piper_voice_config.py` still requires the code default
and `bridge.env.example` to name the same voice, so the two cannot drift apart.

`scripts/test_switch_voice.py` covers the switcher as a black box against a throwaway repo and a
throwaway Hermes config: both surfaces updated by default, each `--scope` honoured, the 22,050 Hz
contract enforced *before* any write, a multi-speaker voice refused without `--speaker`, the
speaker stored as a NAME on the bridge and an ID in Hermes, `bridge.env` comments preserved, a
backup and a state record written per switch, `--dry-run` writing nothing, and `--rollback`
restoring both surfaces.
