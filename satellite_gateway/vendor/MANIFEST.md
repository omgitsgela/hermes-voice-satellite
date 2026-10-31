# Vendored files

Everything here exists so the Gateway does not depend on something that can move or disappear.

| Item | Detail |
|---|---|
| `silero_vad_v6.onnx` | Silero VAD v6, ONNX export, 1,245,151 bytes. `sha256 4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2`. Graph inputs `input (seq_len, 576)`, `h (1,1,128)`, `c (1,1,128)`; outputs `speech_probs`, `hn`, `cn`. |

## Why this one is vendored rather than imported

The rule, 2026-09-25: **"if we're going to use silero, we're going to use it locally, and make
it a repo we track."**

The detector decides whether the device hears the person who owns it. That is not a component to
borrow from a package that can be yanked, or from an asset inside someone else's wheel where an
unrelated upgrade can move or delete it. An earlier candidate path read the model out of
`faster_whisper`'s `assets/` directory, which is exactly the arrangement this avoids: faster_whisper
is our speech-to-text engine, and its next release is under no obligation to keep shipping our voice
detector.

The file is committed here, `gateway/silero_vad.py` loads it by relative path, and
`scripts/test_silero_vad.py` asserts both that it exists and that it is **not** being read from
`site-packages`.

## Where it came from, and how to regenerate it

The upstream project is `https://github.com/snakers4/silero-vad`, tracked as a pinned git bundle in
the mirror's `upstream/` alongside the other five upstreams by
`scripts/refresh-upstream-mirrors.py` (`silero-vad` in its `REPOS` map). That mirror is the source of
record; this copy is what actually runs.

The v6 ONNX export is the model the upstream project publishes for the 16 kHz streaming API, and the
same file is the one `faster_whisper` ships for its own VAD filter, which is why it was available to
copy in the first place. Recorded honestly: **that checksum was verified against the local
`faster_whisper` copy, not against a fresh upstream download on this machine.** If the file is ever
regenerated, `git clone snakers4/silero-vad`, take the v6 ONNX model from its `src/silero_vad/data/`,
and confirm the checksum above or update it deliberately with a note.

The upstream repository also ships a ggml build, which the `pysilero_vad` package uses. It is not
vendored here because it needs `libggml*.so` shared objects as well as the weights, where the ONNX
file is pure data driven by `onnxruntime` — which the Gateway already installs. Fewer moving parts to
own.

## What it must not become

- **Not a network fetch.** Nothing here downloads at runtime. If the model is missing, that is a
  checkout or packaging fault, and `SileroVad.__init__` says so rather than fetching.
- **Not a second copy of the audio.** This directory holds models, not recordings. Room recordings
  are evidence and live in the mirror's `private/endpointer-fixtures/`.
