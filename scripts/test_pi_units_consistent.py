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

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Upstream copies are kept verbatim for diffing and must never be "corrected".
SKIP = ("pi/vendor/", "reference/")

FLAGGED = ("--wake-word-name", "--wake-name", "--custom-model-dir")
# The shim names the wake word in its injected Detection and the satellite names the word it listens
# for. Different flags, one truth: if they disagree, an injected wake logs a wake word the device does
# not use. Checked as a pair below.
WAKE_NAME_FLAGS = ("--wake-word-name", "--wake-name")
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
    """Every value given to `flag` in this file, quoted or bare, ignoring comments.

    Comments in these units discuss flags by name, so scanning raw text makes the guard match its own
    explanation. Only real configuration lines count, and a trailing comment is cut first so that
    `ExecStart=... --flag 'x'   # explains --flag` cannot pick up prose either.
    """
    found = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        code = stripped.split("#", 1)[0]
        for match in re.finditer(rf"{re.escape(flag)}[=\s]+('[^']*'|\"[^\"]*\"|\S+)", code):
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

    # The shim's name and the satellite's name describe the same wake word, so they must agree.
    per_flag: dict[str, set[str]] = {}
    for flag in WAKE_NAME_FLAGS:
        values: set[str] = set()
        for path in units:
            values.update(values_for(path, flag))
        if values:
            per_flag[flag] = values
    if len(per_flag) == len(WAKE_NAME_FLAGS):
        combined = set().union(*per_flag.values())
        if len(combined) > 1:
            detail = "; ".join(f"{flag}={sorted(vals)}" for flag, vals in per_flag.items())
            problems.append(
                f"the shim and the satellite disagree about the wake word name: {detail}")

    # Presence, not just agreement. Removing the flag from a unit leaves the shim on its hardcoded
    # default, which is the very bug this guards against ("ok_nabu" in the log), and a values-only
    # comparison cannot see that because the default is not written down anywhere.
    required = {
        "wake_shim.py": "--wake-name",
        "wyoming-satellite/script/run": "--wake-word-name",
    }
    for marker, flag in required.items():
        carriers = [p for p in units if marker in p.read_text()]
        missing = [p.relative_to(ROOT).as_posix() for p in carriers
                   if not values_for(p, flag)]
        if missing:
            problems.append(
                f"{flag} is missing from a unit that runs {marker}, so it falls back to a "
                f"hardcoded default: {', '.join(missing)}")

    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        return 1
    print("Pi unit files agree: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
