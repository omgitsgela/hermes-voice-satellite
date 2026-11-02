#!/usr/bin/env python3
"""Mirror this repository to the NAS. One-way: GitHub is authoritative, this is the backup.

Why this exists: one copy in one place is a single point of failure. GitHub holds the authoritative
history, this holds a second copy on local hardware, and the live tree at ~/hermes-voice-satellite
is a checkout of the same repository.

Design notes, each of them learned the hard way:
  * the file list comes from `git ls-files`, so the mirror is exactly "what the repo tracks". Nothing
    is matched by filename, because a filename match once wrote the Gateway's client over the
    satellite's core.
  * shutil.copyfile, not copy2: GVFS/SMB rejects the permission and timestamp copies that copy2 does,
    and it raises after the bytes are already written, which looks like a failed copy.
  * every file is verified by size and sha256 rather than trusting the copy call.
  * a `git bundle` carries the whole history in one file, so the backup can be cloned from directly
    if GitHub is ever the thing that dies.
  * GitHub *release assets* (the built installer, the flash bundle, the manifest) are not tracked
    files, so the tracked-file walk cannot see them. They are fetched separately into
    `release-assets/<tag>/` and verified against the sha256 digest GitHub publishes per asset.
    A new release is picked up automatically: the walk is driven by the releases API, not by
    hardcoded names.

Exit codes: 0 all good, 1 something failed (the cron job reports failures only).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from nas_paths import mirror_root

REPO = Path(__file__).resolve().parent.parent
# Both scripts take the mounted parent directory as argv[1] or VOICE_NAS_MOUNT.
# The former VOICE_NAS_MIRROR_ROOT spelling remains an alias; an explicit argument wins.

README_TEXT = """# hermes-voice-satellite (NAS backup)

This is a **one-way backup copy** of the repository
`github.com/omgitsgela/hermes-voice-satellite`. GitHub is the authoritative source; nothing here
should ever be edited by hand, and changes made here are overwritten on the next sync.

Contents:

- the tracked files of the repository, at the commit recorded in `SYNC-INFO.txt`
- `hermes-voice-satellite.bundle`, a complete git history in one file. To restore from it:

      git clone hermes-voice-satellite.bundle hermes-voice-satellite

- `release-assets/<tag>/`, every GitHub release asset (the built installer tarball, the flash
  bundle and its manifest), fetched from the releases API and sha256-verified against GitHub's
  published digests. The list per release is echoed in `SYNC-INFO.txt`.

Kept current by `scripts/mirror-to-nas.py` in the repository, run on a schedule and reported only
when it fails.
"""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _gh_token() -> str:
    return (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()


def list_release_assets(api_base: str = "https://api.github.com") -> list[dict]:
    """Every published release asset: {tag, name, size, sha256, url}.

    GitHub publishes a `sha256:` digest per release asset, so the mirror verifies downloads
    against GitHub's own anchor instead of trusting the transfer. Releases whose assets predate
    digests are still mirrored, verified by size and recorded hash only. The repo is public, so
    plain HTTPS works; the token (cron's env usually has one) only buys rate-limit headroom.
    """
    req_headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": "hermes-voice-satellite-nas-mirror"}
    token = _gh_token()
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    assets: list[dict] = []
    page = 1
    while True:
        url = f"{api_base}/repos/omgitsgela/hermes-voice-satellite/releases?per_page=100&page={page}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=req_headers),
                                    timeout=60) as resp:
            releases = json.load(resp)
        if not releases:
            break
        for rel in releases:
            for a in rel.get("assets", []):
                digest = (a.get("digest") or "").removeprefix("sha256:")
                assets.append({"tag": rel["tag_name"], "name": a["name"],
                               "size": a["size"], "sha256": digest,
                               "url": a["browser_download_url"]})
        # Stop on a short page as well as an empty one: a server that repeats its last page
        # instead of returning [] must not turn this into an endless loop.
        if len(releases) < 100:
            break
        page += 1
    return assets


def download_release_asset(asset: dict, dest: Path) -> None:
    """Fetch to a temp file, verify, then rename into place. A partial GVFS write never becomes
    the visible copy, and a size/hash mismatch is an exception, not a half-file."""
    req_headers = {"User-Agent": "hermes-voice-satellite-nas-mirror"}
    token = _gh_token()
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    tmp = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(urllib.request.Request(asset["url"], headers=req_headers),
                                    timeout=600) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out, 1 << 20)
        actual = sha256(tmp)
        if tmp.stat().st_size != asset["size"]:
            raise RuntimeError(f"size mismatch: got {tmp.stat().st_size}, want {asset['size']}")
        if asset["sha256"] and actual != asset["sha256"]:
            raise RuntimeError(f"sha256 mismatch: got {actual}, GitHub digest says {asset['sha256']}")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def sync_release_assets(nas_dir: Path, problems: list[str]) -> list[str]:
    """Mirror every GitHub release asset under release-assets/<tag>/. Returns one summary line
    per asset (also written to SYNC-INFO, so drift is auditable)."""
    lines: list[str] = []
    try:
        assets = list_release_assets()
    except Exception as exc:
        problems.append(f"release assets: could not list releases: {exc}")
        return lines
    for a in assets:
        # GitHub asset names are single path-free segments; tag comes from GitHub too. Belt and
        # braces anyway: a '/' or '..' in either would walk outside the mirror.
        if "/" in a["name"] or ".." in a["name"] or "/" in a["tag"] or ".." in a["tag"]:
            problems.append(f"release assets: refusing unsafe name {a['tag']!r}/{a['name']!r}")
            continue
        dest = nas_dir / "release-assets" / a["tag"] / a["name"]
        try:
            if dest.is_file() and (not a["sha256"] or sha256(dest) == a["sha256"]):
                lines.append(f"  {a['tag']}/{a['name']} {a['size']} B (unchanged)")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            download_release_asset(a, dest)
            lines.append(f"  {a['tag']}/{a['name']} {a['size']} B sha256 {sha256(dest)}")
        except Exception as exc:
            problems.append(f"release assets: {a['tag']}/{a['name']}: {exc}")
    return lines


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(REPO), capture_output=True,
                          text=True, check=True).stdout


def main() -> int:
    NAS_DIR = mirror_root()
    if NAS_DIR is None:
        print('FAIL: pass the NAS parent directory or set VOICE_NAS_MOUNT', file=sys.stderr)
        return 1
    NAS_ROOT = NAS_DIR.parent
    BUNDLE = NAS_DIR / 'hermes-voice-satellite.bundle'
    README = NAS_DIR / 'README-what-this-is.md'
    if not NAS_ROOT.is_dir():
        print(f"FAIL: the NAS is not mounted at {NAS_ROOT}")
        print("      run: mount-nas")
        return 1

    head = git("rev-parse", "HEAD").strip()
    subject = git("log", "-1", "--format=%ad %s", "--date=short").strip()
    dirty = git("status", "--porcelain").strip()
    tracked = [line for line in git("ls-files").splitlines() if line.strip()]

    NAS_DIR.mkdir(parents=True, exist_ok=True)

    copied = skipped = failed = 0
    problems: list[str] = []
    for rel in tracked:
        src = REPO / rel
        dst = NAS_DIR / rel
        if not src.is_file():
            continue
        try:
            if dst.is_file() and dst.stat().st_size == src.stat().st_size:
                if sha256(dst) == sha256(src):
                    skipped += 1
                    continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            if sha256(dst) == sha256(src):
                copied += 1
            else:
                failed += 1
                problems.append(f"hash mismatch after copy: {rel}")
        except OSError as exc:
            failed += 1
            problems.append(f"{rel}: {exc}")

    # The whole history, in one file.
    bundle_ok = False
    try:
        tmp = Path("/tmp/hermes-voice-satellite.bundle")
        subprocess.run(["git", "bundle", "create", str(tmp), "--all"],
                       cwd=str(REPO), capture_output=True, check=True)
        shutil.copyfile(tmp, BUNDLE)
        bundle_ok = BUNDLE.is_file() and BUNDLE.stat().st_size == tmp.stat().st_size
    except (OSError, subprocess.CalledProcessError) as exc:
        problems.append(f"bundle failed: {exc}")

    # GitHub release assets: the built installer, flash bundle and manifest. These are not
    # tracked files, so the walk above cannot see them; without this the NAS copy silently
    # misses every release. Driven by the releases API, so future releases appear on their own.
    release_lines = sync_release_assets(NAS_DIR, problems)

    README.write_text(README_TEXT)
    (NAS_DIR / "SYNC-INFO.txt").write_text(
        f"repo: github.com/omgitsgela/hermes-voice-satellite\n"
        f"commit: {head}\n"
        f"commit date and subject: {subject}\n"
        f"file count (tracked): {len(tracked)}\n"
        f"bundle: {'ok' if bundle_ok else 'FAILED'}\n"
        f"release assets:\n" + ("\n".join(release_lines) or "  none") + "\n"
        f"live tree dirty at sync time: {'yes' if dirty else 'no'}\n"
    )

    # Quiet on success, on purpose. This runs hourly as a no_agent watchdog job whose stdout is
    # delivered verbatim, so anything printed here becomes a message to the maintainer. Say nothing unless
    # something is actually wrong; every tick is still recorded in the cron run history and in the
    # NAS SYNC-INFO.txt file.
    if failed or problems or not bundle_ok:
        print(f"NAS mirror for hermes-voice-satellite failed at commit {head[:8]}: "
              f"{failed} copy error(s), {len(problems)} problem(s)"
              f"{'' if bundle_ok else ', and the history bundle could not be written'}")
        for p in problems[:10]:
            print(f"  {p}")
        print(f"  source: {REPO}   destination: {NAS_DIR}")
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
