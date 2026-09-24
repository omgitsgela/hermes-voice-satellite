#!/usr/bin/env python3
"""Refresh the upstream mirrors and their bundles (Angela, 2026-09-24).

A single upstream repository disappearing should not be able to stop this project. Each upstream we
depend on is cloned as a bare mirror, bundled, and kept on the NAS beside our own mirror.

Run monthly from the same job that watches the successor repositories. Silent on success unless
something changed or failed, because a monthly notification that says nothing happened is noise.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

MIRRORS = pathlib.Path.home() / "upstream-mirrors"
BUNDLES = MIRRORS / "out"
def _nas_upstream_dir() -> pathlib.Path | None:
    """Find the NAS mirror directory by looking, never by assuming a path.

    A hardcoded answer here has already gone wrong once: the GVFS mount name ends in `,user=<name>`,
    and a path built from a truncated copy of it silently pointed at nothing.
    """
    root = pathlib.Path("/run/user/1000/gvfs")
    for mount in sorted(root.glob("smb-share:server=*")):
        candidate = mount / "AI Projects" / "hermes-voice-satellite" / "upstream"
        if candidate.parent.is_dir():
            return candidate
    return None
REPOS = {
    "wyoming-satellite": "https://github.com/rhasspy/wyoming-satellite",
    "wyoming-openwakeword": "https://github.com/OHF-Voice/wyoming-openwakeword",
    "wyoming": "https://github.com/OHF-Voice/wyoming",
    "seeed-voicecard": "https://github.com/respeaker/seeed-voicecard",
    "linux-voice-assistant": "https://github.com/OHF-Voice/linux-voice-assistant",
}


def sh(*args, cwd=None):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=300)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def main() -> int:
    MIRRORS.mkdir(exist_ok=True)
    BUNDLES.mkdir(exist_ok=True)
    changed, problems = [], []
    for name, url in REPOS.items():
        d = MIRRORS / name
        if not d.exists():
            rc, out = sh("git", "clone", "--mirror", "--quiet", url, str(d))
            if rc:
                problems.append(f"{name}: clone failed: {out[:120]}")
                continue
            changed.append(f"{name}: first mirror")
        else:
            before = sh("git", "-C", str(d), "rev-parse", "HEAD")[1]
            rc, out = sh("git", "-C", str(d), "remote", "update", "--prune")
            if rc:
                problems.append(f"{name}: fetch failed: {out[:120]}")
                continue
            after = sh("git", "-C", str(d), "rev-parse", "HEAD")[1]
            if before != after:
                changed.append(f"{name}: {before[:8]} -> {after[:8]}")
        sh("git", "-C", str(d), "bundle", "create", str(BUNDLES / f"{name}.bundle"), "--all")
        nas = _nas_upstream_dir()
        if nas is not None:
            nas.mkdir(parents=True, exist_ok=True)
            sh("cp", str(BUNDLES / f"{name}.bundle"), str(nas / f"{name}.bundle"))
        else:
            problems.append(f"{name}: NAS mirror directory not found, bundle kept locally only")

    if changed:
        print("upstream mirrors updated: " + "; ".join(changed))
    if problems:
        print("upstream mirror problems: " + "; ".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
