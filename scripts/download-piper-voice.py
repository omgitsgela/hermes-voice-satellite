#!/usr/bin/env python3
"""Download a Piper voice using the installer's pinned SHA-256 trust snapshot."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from satellite_gateway.voice_library import library
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('voice')
parser.add_argument('--download-dir', required=True, type=Path)
args = parser.parse_args()
result = library().download_voice_files(args.voice, args.download_dir)
print(f"{result['voice']}: SHA-256 verified at {result['revision']}")
