#!/usr/bin/env python3
"""Offline regression test for GitHub release-asset mirroring.

The NAS mirror's tracked-file walk cannot see release assets -- they are not tracked files --
which is exactly how the built installer and flash bundle went missing from the NAS copy while
every SYNC-INFO said the mirror was fine. This pins the replacement behaviour without touching
the network: the releases listing, a verified download landing under release-assets/<tag>/, a
digest mismatch leaving NO file behind, and an unchanged asset not re-downloaded.

Run with the Gateway venv:

    .venv/bin/python scripts/test_mirror_release_assets.py
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mirror_to_nas", HERE / "mirror-to-nas.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PAYLOAD = b"the reviewed flash bundle, in miniature\n" * 10
GOOD_SHA = hashlib.sha256(PAYLOAD).hexdigest()

RELEASES = [{
    "tag_name": "v9.9.9",
    "assets": [{
        "name": "flash.bundle.bin",
        "size": len(PAYLOAD),
        "digest": f"sha256:{GOOD_SHA}",
        "browser_download_url": "https://example.invalid/flash.bundle.bin",
    }],
}]


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes):
        super().__init__(body)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def fake_urlopen_for(served: bytes, refetch_is_error: bool = False):
    """Stand in for urllib.request.urlopen: first call answers the releases API, second the
    asset download. With refetch_is_error, an asset fetch raises instead -- used to prove an
    already-mirrored asset is never re-downloaded."""
    calls = {"n": 0}

    def urlopen(request, timeout=None):
        url = request.full_url
        if "/releases" in url:
            calls["n"] += 1
            # Page-aware like the real API: content on page 1, [] on every page after.
            body = RELEASES if calls["n"] == 1 else []
            return FakeResponse(json.dumps(body).encode())
        if refetch_is_error:
            raise AssertionError("unchanged release asset must not be re-fetched")
        return FakeResponse(served)

    return urlopen


failures: list[str] = []

# 1. A clean release asset lands under release-assets/<tag>/ with the right bytes.
with tempfile.TemporaryDirectory() as tmp:
    mirror_dir = pathlib.Path(tmp)
    problems: list[str] = []
    original_urlopen = mod.urllib.request.urlopen
    mod.urllib.request.urlopen = fake_urlopen_for(PAYLOAD)
    try:
        lines = mod.sync_release_assets(mirror_dir, problems)
    finally:
        mod.urllib.request.urlopen = original_urlopen
    landed = mirror_dir / "release-assets" / "v9.9.9" / "flash.bundle.bin"
    if problems:
        failures.append(f"clean sync reported problems: {problems}")
    if not landed.is_file() or landed.read_bytes() != PAYLOAD:
        failures.append("verified asset did not land at release-assets/v9.9.9/flash.bundle.bin")
    if not lines or "flash.bundle.bin" not in lines[0] or GOOD_SHA not in lines[0]:
        failures.append("sync summary line does not name the asset and its hash")

    # 2. Unchanged on the next run: listed as unchanged, not re-fetched. The fake raises if an
    # asset URL is requested, so a re-download would fail the run loudly (and never hit the
    # real network from a test).
    problems2: list[str] = []
    mod.urllib.request.urlopen = fake_urlopen_for(PAYLOAD, refetch_is_error=True)
    try:
        lines2 = mod.sync_release_assets(mirror_dir, problems2)
    finally:
        mod.urllib.request.urlopen = original_urlopen
    if problems2:
        failures.append(f"unchanged sync reported problems: {problems2}")
    if not lines2 or "unchanged" not in lines2[0]:
        failures.append("unchanged asset was not recognised (it would re-download hourly)")

# 3. A tampered download is refused and leaves NOTHING behind -- no partial file posing as a
# release asset, and the problem is reported (the cron job then actually notifies).
with tempfile.TemporaryDirectory() as tmp:
    mirror_dir = pathlib.Path(tmp)
    problems3: list[str] = []
    mod.urllib.request.urlopen = fake_urlopen_for(b"tampered bytes that will not match\n" * 10)
    try:
        mod.sync_release_assets(mirror_dir, problems3)
    finally:
        mod.urllib.request.urlopen = original_urlopen
    if not problems3:
        failures.append("digest mismatch was not reported as a problem")
    walk = [p.relative_to(mirror_dir) for p in mirror_dir.rglob("*") if p.is_file()]
    if walk:
        failures.append(f"digest mismatch left files behind: {walk}")

# 4. Path safety: a malicious asset name cannot walk outside the mirror.
with tempfile.TemporaryDirectory() as tmp:
    mirror_dir = pathlib.Path(tmp)
    escape = [{
        "tag_name": "v0.0.1",
        "assets": [{"name": "../../escape.bin", "size": 2, "digest": "sha256:aa",
                    "browser_download_url": "https://example.invalid/x"}],
    }]

    def escaping_urlopen(request, timeout=None):
        if "/releases" in request.full_url:
            page = "page=1" in request.full_url
            return FakeResponse(json.dumps(escape if page else []).encode())
        raise AssertionError("escape asset must never be fetched")

    problems4: list[str] = []
    mod.urllib.request.urlopen = escaping_urlopen
    try:
        mod.sync_release_assets(mirror_dir, problems4)
    finally:
        mod.urllib.request.urlopen = original_urlopen
    if not problems4:
        failures.append("path-escaping asset name was not refused")

print(f"release-asset mirror checks: {4 - len(failures)}/4 passed")
for f in failures:
    print("  FAIL:", f)
sys.exit(1 if failures else 0)
