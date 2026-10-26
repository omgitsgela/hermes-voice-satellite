#!/usr/bin/env python3
"""Piper voice selection contract: name, speaker, length, and the switching doc.

Changing the voice a household hears must be a documented, testable path, not a
memory exercise. This pins:

1. `bridge/config.py` exposes the voice name AND the multi-speaker pick as
   configuration (`VOICE_PIPER_VOICE`, `VOICE_PIPER_SPEAKER`, `VOICE_PIPER_LENGTH_SCALE`).
2. `bridge/tts.py` resolves a speaker name (`p2769`) or index to a numeric speaker id,
   fails loudly on a name that is not in the voice's own `speaker_id_map`, and passes
   the resolved config on EVERY synthesis path (`synthesize_chunks`, `synthesize_to_wav`).
3. A missing voice json (fresh clone, voice not downloaded yet) must not crash the
   bridge at import time; resolution happens when the voice loads.
4. `docs/switching-voices.md` exists, names its stage, and names the real knobs and
   the real speaker-listing tool, so the procedure is findable from the map.
5. The SHIPPED default is pinned as a contract, not as a favourite voice: it must be a
   plausible catalog-style name that agrees with `bridge.env.example`. Which voice this
   installation actually speaks with is a config decision made with
   `scripts/switch-voice.py`, and changing it must never require editing this file.

    .venv/bin/python scripts/test_piper_voice_config.py
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures: list[str] = []
checks = 0


def check(label: str, condition) -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(label)


def load_config(env: dict | None = None):
    """Exec bridge/config.py fresh with a controlled environment and no real bridge.env."""
    source = (ROOT / "bridge/config.py").read_text()
    namespace = {"__file__": str(ROOT / "bridge/config.py")}
    with patch.dict(os.environ, env or {}, clear=True), \
         patch.object(Path, "read_text", return_value=""):
        exec(compile(source, "bridge/config.py", "exec"), namespace)
    return namespace


# --- 1. configuration surface -------------------------------------------------

cfg = load_config()
check("default voice is a plausible Piper catalog name",
      bool(re.fullmatch(r"(en_[A-Z]{2}-[A-Za-z0-9_]+-(low|medium|high))|([a-z0-9][a-z0-9_.-]*)",
                        cfg["PIPER_VOICE_NAME"])),
      )
# The shipped default is a code decision (what a fresh clone gets). The voice this installation
# actually speaks with lives in bridge.env and changes with scripts/switch-voice.py, so this test
# must not encode anyone's current preference. It pins the pairing instead: whatever the code
# default is, bridge.env.example documents the same name.
_example = (ROOT / "bridge.env.example").read_text()
_doc_default = re.search(r"^#\s*VOICE_PIPER_VOICE=(\S+)", _example, re.M)
check("bridge.env.example documents the code default",
      _doc_default is not None and _doc_default.group(1) == cfg["PIPER_VOICE_NAME"])
check("VOICE_PIPER_SPEAKER is read as configuration", "PIPER_SPEAKER_SETTING" in cfg)
# A multi-speaker default would have to name a speaker, or Piper silently picks one from the
# corpus. The shipped default is single-speaker, so "no override" is its correct default; the
# multi-speaker behaviour itself is pinned below.
check("the shipped default voice needs no speaker override",
      cfg.get("PIPER_SPEAKER_SETTING") == "")
check("VOICE_PIPER_LENGTH_SCALE is read as configuration", "PIPER_LENGTH_SCALE" in cfg)

cfg = load_config({"VOICE_PIPER_VOICE": "en_US-amy-medium", "VOICE_PIPER_SPEAKER": ""})
check("an explicit empty speaker means 'no speaker override'",
      cfg["PIPER_VOICE_NAME"] == "en_US-amy-medium" and cfg["PIPER_SPEAKER_SETTING"] == "")
# config import must not explode when the voice json does not exist yet (fresh clone), and
# must map a configured name to the voices/ paths by pure string formatting:
check("config import does not resolve against the filesystem",
      Path(cfg["VOICE_MODEL"]).name == "en_US-amy-medium.onnx"
      and Path(cfg["VOICE_CONFIG"]).name == "en_US-amy-medium.onnx.json")

# --- 2. speaker resolution ----------------------------------------------------

from bridge import tts

with tempfile.TemporaryDirectory() as td:
    fake = Path(td) / "fake.onnx.json"
    fake.write_text(json.dumps({
        "audio": {"sample_rate": 22050, "quality": "high"},
        "num_speakers": 245,
        "speaker_id_map": {"p3922": 0, "p2769": 16, "p64": 2, "78": 36},
    }))

    check("speaker name resolves through the voice's own map",
          tts.resolve_speaker_id("p2769", fake) == 16)
    check("a name that LOOKS numeric still resolves through the map first (clean100's '78' -> 36, "
          "not numeric id 78)",
          tts.resolve_speaker_id("78", fake) == 36)
    check("numeric index passes through", tts.resolve_speaker_id("2", fake) == 2)
    check("zero is a valid id, not 'unset'", tts.resolve_speaker_id("0", fake) == 0)
    check("empty setting means no override", tts.resolve_speaker_id("", fake) is None)
    check("None setting means no override", tts.resolve_speaker_id(None, fake) is None)

    for bad in ("p9999", "not-a-speaker", "abc5", "-1", "1.5", "99999"):
        try:
            tts.resolve_speaker_id(bad, fake)
            failures.append(f"bad speaker {bad!r} was accepted")
        except ValueError:
            checks += 1

    missing = Path(td) / "missing.onnx.json"
    try:
        tts.resolve_speaker_id("p2769", missing)
        failures.append("a missing voice json was silently tolerated")
    except FileNotFoundError:
        checks += 1

    # --- 3. every synthesis path carries the speaker --------------------------
    with patch.object(tts, "PIPER_VOICE_MODEL_CONFIG_PATH", fake), \
         patch.object(tts, "PIPER_SPEAKER_SETTING", "p64"), \
         patch.object(tts, "PIPER_LENGTH_SCALE", None):
        syn = tts.build_synthesis_config()
        check("build_synthesis_config resolves the configured speaker",
              syn.speaker_id == 2)

    with patch.object(tts, "PIPER_VOICE_MODEL_CONFIG_PATH", fake), \
         patch.object(tts, "PIPER_SPEAKER_SETTING", ""), \
         patch.object(tts, "PIPER_LENGTH_SCALE", 0.95):
        syn = tts.build_synthesis_config()
        check("length scale override reaches Piper",
              dataclasses.asdict(syn).get("length_scale") == 0.95)
        check("no speaker setting leaves speaker unset", syn.speaker_id is None)

    # Behaviour, not source-reading: drive the real functions with a Piper whose synthesize
    # is recorded, and require the resolved speaker to arrive in it.
    captured: dict = {}

    class _FakeChunk:
        sample_rate, sample_width, sample_channels = 22050, 2, 1

        def __init__(self):
            self.audio_int16_bytes = b"\x00\x01" * 8

    class _FakeVoice:
        def synthesize(self, text, syn_config=None, include_alignments=False):
            captured["chunks_syn_config"] = syn_config
            return iter([_FakeChunk()])

        def synthesize_wav(self, text, wav_file, syn_config=None, set_wav_format=True,
                           include_alignments=False):
            captured["wav_syn_config"] = syn_config
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00\x01" * 8)

    with patch.object(tts, "load_voice", return_value=_FakeVoice()), \
         patch.object(tts, "PIPER_VOICE_MODEL_CONFIG_PATH", fake), \
         patch.object(tts, "PIPER_SPEAKER_SETTING", "p3922"), \
         patch.object(tts, "PIPER_LENGTH_SCALE", None):
        list(tts.synthesize_chunks("hello"))
        syn = captured.get("chunks_syn_config")
        check("synthesize_chunks passes the resolved speaker through Piper",
              syn is not None and syn.speaker_id == 0)
        wav_path = Path(td) / "out.wav"
        tts.synthesize_to_wav("hello", wav_path)
        syn = captured.get("wav_syn_config")
        check("synthesize_to_wav passes the resolved speaker through Piper",
              syn is not None and syn.speaker_id == 0)

sig = inspect.signature(tts.PiperVoice.synthesize)
check("piper's synthesize accepts syn_config", "syn_config" in sig.parameters)

# --- 4. the switching document is real and names the real knobs ---------------

doc = ROOT / "docs" / "switching-voices.md"
check("docs/switching-voices.md exists", doc.is_file())
if doc.is_file():
    text = doc.read_text()
    check("it names its stage in the pipeline", "**Where this sits:**" in text)
    for knob in ("VOICE_PIPER_VOICE", "VOICE_PIPER_SPEAKER", "VOICE_PIPER_LENGTH_SCALE"):
        check(f"doc names the knob {knob}", knob in text)
    check("doc points at the speaker-listing tool", "scripts/list-piper-speakers.py" in text)
    check("doc points at the external-voice fetcher", "scripts/fetch-voice.py" in text)
    check("doc tells the reader voices/ is git-ignored", "git-ignored" in text or "not committed" in text)

check("the speaker-listing tool exists", (ROOT / "scripts/list-piper-speakers.py").is_file())

# The installer must follow the configured voice, not a hardcoded one.
installer = (ROOT / "scripts/install-gateway.sh").read_text()
check("install-gateway.sh reads the configured voice name",
      "VOICE_PIPER_VOICE" in installer)

print(f"piper voice config: {checks - len(failures)}/{checks} passed")
for failure in failures:
    print(f"  FAIL: {failure}")
raise SystemExit(1 if failures else 0)
