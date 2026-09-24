#!/usr/bin/env python3
"""Repository check: the tracked unit files must not contradict each other.

Several unit files exist in two layouts (the legacy single-file copies under `pi/`, and the authoritative
bundle plus drop-in under `pi/systemd/`, which is what the hardware docs tell you to install). Both are
tracked, so an edit to one and not the other produces a repository that describes two different devices.

This was not hypothetical: on 2026-09-24 the legacy satellite copy still specified the retired `ok_nabu`
wake word and the old awake-tone mute, and the legacy wake-service copy was missing the custom model
directory, so installing from them would have broken the wake word on a working device.

The check compares values rather than bytes, because the two layouts are legitimately not identical: the
bundles carry an install-path header and append their drop-in. Consolidating the duplicated copies is
issue #45; until then, this keeps them honest.

Run with the repository's interpreter: .venv/bin/python scripts/test_pi_units_consistent.py
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Upstream copies are kept verbatim for diffing and must never be "corrected".
SKIP = ("pi/vendor/", "reference/")

FLAGGED = ("--wake-word-name", "--custom-model-dir")
# Flags the device must not carry at all; see issue #38 for why a --vad drop-in is a trap here.
FORBIDDEN = ("--vad",)


def tracked_units() -> list[pathlib.Path]:
    out = []
    for pattern in ("*.service", "*.conf"):
        for path in sorted(ROOT.glob(f"pi/**/{pattern}")):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith(SKIP):
                continue
            out.append(path)
    return out


def values_for(path: pathlib.Path, flag: str) -> list[str]:
    """Every value given to `flag` in this file, quoted or bare."""
    found = []
    for match in re.finditer(rf"{re.escape(flag)}[=\s]+('[^']*'|\"[^\"]*\"|\S+)", path.read_text()):
        found.append(match.group(1).strip("'\""))
    return found


def main() -> int:
    problems: list[str] = []
    units = tracked_units()
    print(f"unit files checked: {len(units)}")

    for flag in FLAGGED:
        seen: dict[str, list[str]] = {}
        for path in units:
            for value in values_for(path, flag):
                seen.setdefault(value, []).append(path.relative_to(ROOT).as_posix())
        if len(seen) > 1:
            detail = "; ".join(f"{value!r} in {', '.join(paths)}" for value, paths in seen.items())
            problems.append(f"{flag} has conflicting values across tracked units: {detail}")
        elif seen:
            value = next(iter(seen))
            print(f"  {flag}: {value} (agreed by {len(next(iter(seen.values())))} file(s))")
        else:
            problems.append(f"{flag} is not set anywhere, which cannot be right")

    for flag in FORBIDDEN:
        offenders = [p.relative_to(ROOT).as_posix() for p in units if flag in p.read_text()]
        if offenders:
            problems.append(f"{flag} appears in tracked unit files: {', '.join(offenders)}")

    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        return 1
    print("Pi unit files agree: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
