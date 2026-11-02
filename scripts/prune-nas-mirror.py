#!/usr/bin/env python3
"""Remove files from the NAS mirror that the repository no longer tracks.

The mirror copies tracked files one by one and verifies each, which is deliberate and works. What it
never did was notice a file disappearing: delete something from the repository and the copy stayed
behind. A stale skills/ tree survived that way until it was found by hand.

Runs after the mirror. Dry by default; --apply actually deletes, which is what the cron wrapper uses.
Silent when there is nothing to do, because a notification about nothing is noise.

Nothing outside the repository own tracked files is ever touched. private/, upstream/,
release-assets/ and the mirror own generated files live inside the mirror folder and must survive
every prune: the device images, the enclosure files under a no-redistribution licence, the upstream
bundles, and the GitHub release assets (built installer, flash bundle, manifest) that the mirror
fetches separately because they are not tracked files.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

from nas_paths import mirror_root as configured_mirror_root

PROTECTED_TOP = {"private", "upstream", "release-assets", ".git"}
PROTECTED_NAMES = {"hermes-voice-satellite.bundle", "SYNC-INFO.txt", "README-what-this-is.md"}
REPO = pathlib.Path(__file__).resolve().parent.parent


def mirror_root():
    """Use the same explicit parent directory as the mirror; never pick another share."""
    return configured_mirror_root(allow_apply=True)


def tracked_files():
    result = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                            capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip() or f"git exited {result.returncode}"
        print("prune: git ls-files failed: " + detail, file=sys.stderr)
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def main():
    apply = "--apply" in sys.argv
    mirror = mirror_root()
    if mirror is None:
        print("prune: pass the NAS parent directory or set VOICE_NAS_MOUNT", file=sys.stderr)
        return 1
    # The sanity anchor is the mirror's own generated marker, not a tracked code directory:
    # code directories rename (gateway/ became satellite_gateway/) but SYNC-INFO.txt is
    # rewritten by every successful mirror run, so a missing marker means this is not a
    # mirror we are allowed to prune.
    if not (mirror / 'SYNC-INFO.txt').is_file():
        print(f"prune: NAS mirror not found at {mirror}, nothing done", file=sys.stderr)
        return 1
    tracked = tracked_files()
    if tracked is None:
        return 1
    extras = []
    for path in mirror.rglob("*"):
        rel = path.relative_to(mirror)
        if rel.parts and rel.parts[0] in PROTECTED_TOP:
            continue
        if rel.name in PROTECTED_NAMES:
            continue
        if path.is_file() and str(rel) not in tracked:
            extras.append(rel)
    if not extras:
        return 0
    for rel in sorted(extras, key=lambda p: len(p.parts), reverse=True):
        target = mirror / rel
        if apply:
            try:
                target.unlink()
                parent = target.parent
                while parent != mirror and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
            except OSError as exc:
                print("prune: could not remove " + str(rel) + ": " + type(exc).__name__,
                      file=sys.stderr)
        print(("pruned: " if apply else "would prune: ") + str(rel))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
