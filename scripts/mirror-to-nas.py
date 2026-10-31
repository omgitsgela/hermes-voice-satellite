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

Exit codes: 0 all good, 1 something failed (the cron job reports failures only).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from nas_paths import mirror_root

REPO = Path(__file__).resolve().parent.parent
# Both scripts take the mounted parent directory as argv[1] or VOICE_NAS_MOUNT.
# The former VOICE_NAS_MIRROR_ROOT spelling remains an alias; an explicit argument wins.

README_TEXT = """# hermes-voice-satellite (NAS backup)

This is a **one-way backup copy** of a private repository
`github.com/omgitsgela/hermes-voice-satellite`. GitHub is the authoritative source; nothing here
should ever be edited by hand, and changes made here are overwritten on the next sync.

Contents:

- the tracked files of the repository, at the commit recorded in `SYNC-INFO.txt`
- `hermes-voice-satellite.bundle`, a complete git history in one file. To restore from it:

      git clone hermes-voice-satellite.bundle hermes-voice-satellite

Kept current by `scripts/mirror-to-nas.py` in the repository, run on a schedule and reported only
when it fails.
"""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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

    README.write_text(README_TEXT)
    (NAS_DIR / "SYNC-INFO.txt").write_text(
        f"repo: github.com/omgitsgela/hermes-voice-satellite\n"
        f"commit: {head}\n"
        f"commit date and subject: {subject}\n"
        f"file count (tracked): {len(tracked)}\n"
        f"bundle: {'ok' if bundle_ok else 'FAILED'}\n"
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
