#!/usr/bin/env python3
"""List the speakers a Piper voice carries, name -> numeric id.

Multi-speaker Piper voices (clean100: 245 speakers; en_US-libritts-high: 904) are unusable
without a chosen speaker; this is the lookup that turns "I liked a sample" into a
VOICE_PIPER_SPEAKER value. Reads the voice's own speaker_id_map from its .onnx.json, so
what it prints is exactly what the Gateway can resolve.

  .venv/bin/python scripts/list-piper-speakers.py                  # configured voice
  .venv/bin/python scripts/list-piper-speakers.py --voice clean100
  .venv/bin/python scripts/list-piper-speakers.py --voice /path/to/some.onnx.json
  .venv/bin/python scripts/list-piper-speakers.py --search 2769    # substring over names

See docs/switching-voices.md for the full voice-change procedure.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def resolve_config_path(voice: str | None) -> Path:
    if voice is None:
        from satellite_gateway.config import VOICE_CONFIG

        return VOICE_CONFIG
    p = Path(voice).expanduser()
    if p.suffix == ".json":
        return p
    return ROOT / "voices" / f"{voice}.onnx.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="List Piper voice speakers (name -> id).")
    parser.add_argument("--voice", help="Voice name (in voices/) or a path to a .onnx.json. "
                                        "Default: the voice configured in satellite-gateway.env.")
    parser.add_argument("--search", help="Only names containing this substring.")
    parser.add_argument("--limit", type=int, default=0, help="Show at most N speakers.")
    args = parser.parse_args()

    config_path = resolve_config_path(args.voice)
    if not config_path.is_file():
        print(f"no voice config at {config_path}", file=sys.stderr)
        print("download it first: .venv/bin/python -m piper.download_voices "
              "--download-dir voices <name> (catalog voice) or "
              ".venv/bin/python scripts/fetch-voice.py <name> (curated voice)", file=sys.stderr)
        return 1

    config = json.loads(config_path.read_text())
    speaker_map = config.get("speaker_id_map", {})
    num_speakers = config.get("num_speakers", len(speaker_map))
    if num_speakers <= 1:
        print(f"{config_path.name}: single-speaker voice; VOICE_PIPER_SPEAKER does not apply")
        return 0

    names = sorted(speaker_map.items(), key=lambda item: item[1])
    if args.search:
        names = [(n, i) for n, i in names if args.search in n]
    shown = names[: args.limit] if args.limit > 0 else names

    print(f"{config_path.name}: {num_speakers} speakers "
          f"(sample_rate {config['audio']['sample_rate']}, quality "
          f"{config['audio'].get('quality', '?')})")
    for name, sid in shown:
        print(f"  {name} -> {sid}")
    if len(names) > len(shown):
        print(f"  ... {len(names) - len(shown)} more; narrow with --search")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
