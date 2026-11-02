#!/usr/bin/env python3
"""Refresh published free wake-source checksums; never contacts a satellite."""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packages/installer/src'))
from hermes_installer.catalogue import WAKE_WORDS
hashes = {}
for entry in WAKE_WORDS:
    if not entry.source_url.startswith('https:'):
        continue
    with urllib.request.urlopen(entry.source_url, timeout=60) as response:
        data = response.read()
    hashes[entry.source_url] = hashlib.sha256(data).hexdigest()
(ROOT / 'packages/installer/src/hermes_installer/wake-checksums.json').write_text(json.dumps(hashes, indent=2, sort_keys=True) + '\n')
print(f'Recorded {len(hashes)} free wake-model SHA-256 checksums.')
