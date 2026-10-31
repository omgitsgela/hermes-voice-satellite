#!/usr/bin/env python3
"""Fetch a Piper voice that is NOT in the Piper catalog, with integrity pins.

`piper.download_voices` only knows voices published in the HuggingFace voices release and
only accepts names shaped `<language>-<name>-<quality>`. Some good voices live elsewhere —
for example `clean100` from brycebeattie.com (245 speakers, CC BY 4.0), which Piper's own
downloader rejects by name pattern. Those voices need a fetch path that:

  * pins the exact bytes (sha256 per file), so an installation can never silently speak with
    whatever the host URL serves on the day,
  * enforces the audio contract before installing (22,050 Hz — see docs/audio-fidelity.md;
    every Piper English voice emits it, and the satellite's format contract depends on it),
  * lands files under exactly the names `satellite_gateway/config.py` builds (`{VOICE_PIPER_VOICE}.onnx`
    + `.onnx.json`), so the rest of the system does not care where a voice came from,
  * is idempotent: re-running hash-checks what is already there instead of refetching.

Usage:
  .venv/bin/python scripts/fetch-voice.py clean100 [--download-dir voices]
  .venv/bin/python scripts/fetch-voice.py --is-external clean100   # exit 0: curated here
  .venv/bin/python scripts/fetch-voice.py --list

--is-external is what scripts/install-gateway.sh branches on: a curated name goes through
this script, anything else goes through piper.download_voices as before.

Adding a voice: put its URL + sha256 of the exact published bytes in EXTERNAL_VOICES below,
with the license its author states. Verify the hashes yourself against the published files
before committing them (sha256sum of the downloaded originals), and update THIRD-PARTY.md and
docs/switching-voices.md in the same change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

# The 22,050 Hz contract. A voice at any other rate breaks the satellite's audio format
# (docs/audio-fidelity.md) and must be rejected at fetch time, not at first synthesis.
REQUIRED_SAMPLE_RATE = 22050

# Curated external voices: name -> license note + files with url and the sha256 of the
# published bytes. The file keys MUST match the names satellite_gateway/config.py builds from
# VOICE_PIPER_VOICE. Hashes pinned 2026-10-26 against the published files.
EXTERNAL_VOICES = {
    "clean100": {
        "license": ("CC BY 4.0 (LibriTTS-r 'Clean 100' dataset, openslr.org/141); author "
                    "Bryce Beattie imposes no further restriction — brycebeattie.com/files/tts"),
        "speakers": 245,
        "files": {
            "clean100.onnx": {
                "url": "https://sfo3.digitaloceanspaces.com/bkmdls/clean100.onnx",
                "sha256": "a48b44084f5465a9d1c5e74c46c6ce6e1907af85e34f15292b9f44b13263f6e4",
            },
            "clean100.onnx.json": {
                "url": "https://sfo3.digitaloceanspaces.com/bkmdls/clean100.onnx.json",
                "sha256": "575a90e0f03154d30c4e8d9b1a86dcb590f7df2fc65e24466dde3c91491984b8",
            },
        },
    },
}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_config_contract(config_path: Path) -> int:
    """Validate the fetched voice json against the audio contract. Returns speaker count.

    Raises ValueError on anything the satellite cannot speak with.
    """
    config = json.loads(config_path.read_text())
    rate = int(config.get("audio", {}).get("sample_rate", 0))
    if rate != REQUIRED_SAMPLE_RATE:
        raise ValueError(
            f"{config_path.name} declares sample_rate {rate}; this deployment requires "
            f"{REQUIRED_SAMPLE_RATE} Hz (see docs/audio-fidelity.md)")
    return int(config.get("num_speakers", 1))


def fetch_voice(name: str, download_dir: Path) -> int:
    entry = EXTERNAL_VOICES.get(name)
    if entry is None:
        curated = ", ".join(sorted(EXTERNAL_VOICES)) or "(none)"
        print(f"'{name}' is not a curated external voice (curated: {curated}). If it is a "
              f"Piper catalog voice, use: .venv/bin/python -m piper.download_voices "
              f"--download-dir {download_dir} {name}", file=sys.stderr)
        return 2

    download_dir.mkdir(parents=True, exist_ok=True)

    # Fetch to .part files first: a refusal mid-run must not leave a half-voice installed.
    staged: list[tuple[Path, Path]] = []
    try:
        for fname, spec in entry["files"].items():
            target = download_dir / fname
            want = spec["sha256"]
            if target.is_file() and sha256_of(target) == want:
                print(f"  {fname}: already installed, sha256 verified")
                continue
            part = target.with_suffix(target.suffix + ".part")
            print(f"  {fname}: fetching (pinned sha256 {want[:12]}...)")
            with urllib.request.urlopen(spec["url"], timeout=120) as resp, part.open("wb") as out:
                while True:
                    block = resp.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
            got = sha256_of(part)
            if got != want:
                print(f"  {fname}: sha256 MISMATCH (got {got[:12]}..., wanted {want[:12]}...). "
                      f"The published bytes changed or the download was corrupted; refusing to "
                      f"install. Re-verify the source and update the pinned hash deliberately.",
                      file=sys.stderr)
                part.unlink(missing_ok=True)
                for staged_part, _ in staged:
                    staged_part.unlink(missing_ok=True)
                return 3
            staged.append((part, target))

        # Validate the contract on the staged json BEFORE anything lands in voices/.
        json_name = next((f for f in entry["files"] if f.endswith(".onnx.json")), None)
        config_staged = next((p for p, t in staged if t.name == json_name), None)
        config_target = download_dir / json_name if json_name else None
        config_path = config_staged if config_staged is not None else config_target
        speakers = int(entry.get("speakers", 1))
        if config_path is not None:
            speakers = check_config_contract(config_path)

        for part, target in staged:
            part.replace(target)
        print(f"  installed into {download_dir} ({entry['license']})")
        if speakers > 1:
            print(f"  multi-speaker voice ({speakers} speakers): pick one with "
                  f"scripts/list-piper-speakers.py and set VOICE_PIPER_SPEAKER")
        return 0
    except (OSError, ValueError) as exc:
        print(f"fetch-voice failed: {exc}", file=sys.stderr)
        for part, _ in staged:
            part.unlink(missing_ok=True)
        return 4


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a curated, hash-pinned external Piper voice.")
    parser.add_argument("voice", nargs="?", help="Voice name in the curated catalog (see --list).")
    parser.add_argument("--download-dir", default=str(Path(__file__).resolve().parent.parent / "voices"),
                        help="Where voice files land (default: the repo's voices/).")
    parser.add_argument("--is-external", metavar="NAME", action="store", default=None,
                        help="Exit 0 if NAME is curated here, 1 otherwise (installer branching hook).")
    parser.add_argument("--list", action="store_true", help="List curated voices.")
    args = parser.parse_args()

    if args.is_external is not None:
        return 0 if args.is_external in EXTERNAL_VOICES else 1

    if args.list:
        for name, entry in sorted(EXTERNAL_VOICES.items()):
            print(f"{name}: {entry.get('speakers', '?')} speakers; {entry['license']}")
        return 0

    if not args.voice:
        parser.error("a voice name is required (or --list / --is-external)")
    return fetch_voice(args.voice, Path(args.download_dir).expanduser())


if __name__ == "__main__":
    raise SystemExit(main())
