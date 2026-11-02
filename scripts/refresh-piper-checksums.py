#!/usr/bin/env python3
"""Refresh the offline SHA-256 trust snapshot from a pinned Piper library commit.

Models use published Git LFS SHA-256 IDs. Small configuration files are authenticated
against their Git blob IDs before recording SHA-256. No model weights are downloaded.
"""
import concurrent.futures
import hashlib
import json
import urllib.request
import urllib.parse
from pathlib import Path

BASE = 'https://huggingface.co'
REPO = 'rhasspy/piper-voices'


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read(), response.headers.get('Link', '')


def main():
    revision = json.loads(fetch(f'{BASE}/api/models/{REPO}')[0])['sha']
    url = f'{BASE}/api/models/{REPO}/tree/{revision}?recursive=true&limit=1000'
    entries = []
    while url:
        data, link = fetch(url)
        entries.extend(json.loads(data))
        url = link.split('>')[0].lstrip('<') if 'rel="next"' in link else ''
    hashes = {e['path']: e['lfs']['oid'] for e in entries
              if e['path'].endswith('.onnx') and e.get('lfs')}
    configs = [e for e in entries if e['path'].endswith('.onnx.json')]
    def config_hash(entry):
        data, _ = fetch(f"{BASE}/{REPO}/resolve/{revision}/{urllib.parse.quote(entry['path'])}")
        blob = b'blob ' + str(len(data)).encode() + b'\0' + data
        assert hashlib.sha1(blob).hexdigest() == entry['oid'], entry['path']
        return entry['path'], hashlib.sha256(data).hexdigest()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        hashes.update(pool.map(config_hash, configs))
    target = Path(__file__).resolve().parents[1] / 'packages/installer/src/hermes_installer/piper-checksums.json'
    target.write_text(json.dumps({'revision': revision, 'sha256': hashes}, indent=2, sort_keys=True) + '\n')
    print(f'Recorded {len(hashes)} SHA-256 checksums at {revision}; no model weights downloaded.')


if __name__ == '__main__':
    main()
