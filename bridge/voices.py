"""Voice metadata and speaker resolution, with no Piper/onnxruntime dependency.

`bridge/tts.py` needs the speaker resolution at synthesis time; `scripts/switch-voice.py` needs
the same rules *before* it writes any configuration, and it should not have to load onnxruntime
to read a JSON file. Keeping the rules here means there is exactly one implementation of
"what does this speaker setting mean", which is the piece that has gone wrong before:
multi-speaker voices number their speakers by an internal id while their map is keyed by a name,
and the two are not interchangeable.
"""

from __future__ import annotations

import json
from pathlib import Path

# The satellite plays 22050 Hz 16-bit mono (see SND_FORMAT in bridge/config.py). Every published
# Piper English voice is 22050, so a voice at any other rate is a contract violation, not a
# preference: nothing resamples on the device.
VOICE_SAMPLE_RATE = 22050


def load_voice_config(config_path) -> dict:
    """Read a voice's `.onnx.json`. Raises FileNotFoundError with the actionable message."""
    path = Path(config_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"voice config {path.name} not found; download the voice before starting the "
            f"bridge (see docs/switching-voices.md)")
    return json.loads(path.read_text())


def describe_voice(config_path) -> dict:
    """The three facts a caller needs about a voice, or a clear failure.

    Raises ValueError when the sample rate is not the satellite's contract, because a voice
    that cannot be played natively must be rejected before it is installed, not after.
    """
    config = load_voice_config(config_path)
    audio = config.get("audio") or {}
    sample_rate = audio.get("sample_rate")
    if sample_rate != VOICE_SAMPLE_RATE:
        raise ValueError(
            f"voice is {sample_rate} Hz but the satellite plays {VOICE_SAMPLE_RATE} Hz natively; "
            f"refusing it (no resampling happens on the device)")
    speaker_map = config.get("speaker_id_map") or {}
    return {
        "sample_rate": sample_rate,
        "quality": audio.get("quality"),
        "num_speakers": int(config.get("num_speakers") or len(speaker_map) or 1),
        "speaker_id_map": speaker_map,
        "language": (config.get("language") or {}).get("code")
        or (config.get("espeak") or {}).get("voice"),
    }


def resolve_speaker_id(setting, config_path: Path):
    """Turn a configured speaker into the numeric id Piper wants.

    Accepts a name from the voice's own speaker_id_map ("p2769") or a plain numeric id.
    Names are preferred over indices because Piper voices have been re-released with speaker
    lists in a different order; a name cannot land on the wrong person after a re-download.

    Raises ValueError on anything the voice cannot honour (a name not in its map, a negative,
    fractional, or nonnumeric token) rather than letting Piper fall to its default speaker in
    silence -- the household would just hear someone else. Raises FileNotFoundError when the
    voice's .onnx.json is absent, which is the "download the voice first" answer, not a crash
    in the audio path. Empty/None means no override: Piper's own default speaker.
    """
    if setting is None or str(setting).strip() == "":
        return None
    setting = str(setting).strip()

    if not Path(config_path).is_file():
        raise FileNotFoundError(
            f"voice config {Path(config_path).name} not found; download the voice before "
            f"starting the bridge (see docs/switching-voices.md)")

    config = json.loads(Path(config_path).read_text())
    speaker_map = config.get("speaker_id_map", {})

    if setting in speaker_map:
        return int(speaker_map[setting])
    try:
        index = int(setting)
    except ValueError:
        raise ValueError(
            f"speaker {setting!r} is not a name in {Path(config_path).name}'s speaker_id_map "
            f"({len(speaker_map)} speakers) and is not a numeric id; list them with "
            f"scripts/list-piper-speakers.py") from None
    num_speakers = int(config.get("num_speakers", len(speaker_map) or 1))
    if index < 0 or (num_speakers > 1 and index >= num_speakers):
        raise ValueError(
            f"speaker id {index} is outside this voice's range (0..{num_speakers - 1})")
    return index
