#!/usr/bin/env python3
"""Remove files from the NAS mirror that the repository no longer tracks.

The mirror copies tracked files one by one and verifies each, which is deliberate and works. What it
never did was notice a file disappearing: delete something from the repository and the copy stayed
behind. A stale `skills/` tree survived that way until it was found by hand.

Runs after the mirror, dry by default. `--apply` actually deletes, which is what the cron wrapper
uses. Silent when there is nothing to do, because a monthly message saying nothing happened is noise.

Nothing outside the repository's own tracked directories is ever touched. `private/`, `upstream/` and
the mirror's own generated files live inside the mirror folder and must survive every prune: they are
the device images, the enclosure files under a no-redistribution licence, and the upstream bundles.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

PROTECTED = {"private", "upstream", ".git",
             "hermes-voice-satellite.bundle", "SYNC-INFO.txt", "README-what-this-is.md"}
REPO = pathlib.Path.home() / "hermes-voice-bridge"


def mirror_root() -> pathlib.Path | None:
    """Find the NAS mirror by looking, never by assuming a path."""
    for mount in sorted(pathlib.Path("/run/user/1000/gvfs").glob("smb-share:server=*")):
        candidate = mount / "AI Projects" / "hermes-voice-satellite"
        if candidate.is_dir() and (candidate / "bridge").is_dir():
            return candidate
    return None


def main() -> int:
    apply = "--apply" in sys.argv
    mirror = mirror_root()
    if mirror is None:
        print("prune: NAS mirror not found, nothing done", file=sys.stderr)
        return 1

    tracked = set()
    for line in subprocess.run(["git", "-C", str(REPO), "ls-files"],
                               capture_output=True, text=True).stdout.split("
"):
        if line.strip():
            tracked.add(line.strip())

    extras = []
    for path in mirror.rglob("*"):
        rel = path.relative_to(mirror)
        if rel.parts and rel.parts[0] in PROTECTED:
            continue
        if rel.name in PROTECTED:
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
            except Exception as exc:
                print(f"prune: could not remove {rel}: {type(exc).__name__}", file=sys.stderr)
        print(("pruned: " if apply else "would prune: ") + str(rel))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
