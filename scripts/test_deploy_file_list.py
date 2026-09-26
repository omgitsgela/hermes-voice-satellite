#!/usr/bin/env python3
"""Offline check that the card deploy script ships every device module.

Why this exists: `scripts/deploy-to-card.sh` carried a hardcoded list of four modules, so
`wake_shim.py` was never copied to a device no matter how many times the script ran. The fix
for issue #17 was merged, tested, and its issue closed, while the device kept running the
previous copy for three days. Nothing failed, because nothing checked.

So this pins two things:

* the list of modules the script copies equals the set of device modules in `pi/`, derived
  from the directory rather than restated here, so adding a module to `pi/` without adding it
  to the script fails this test instead of silently shipping nothing;
* both occurrences of the list (the copy loop and the verification loop) agree, because a
  script that copies a file and then does not verify it is how a partial deploy goes unnoticed.

`DEVICE_EXCLUSIONS` is the deliberate part: a file listed there is a conscious decision that it
is not a deployed example module. Adding to it should be as hard as adding to the list itself.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "deploy-to-card.sh"
PI = ROOT / "pi"

# Not deployed as an examples/ module, and each for a stated reason:
#   satellite.py          the vendored upstream copy; ours ships as a patch in pi/vendor/
#   test_led_controller.py  device-side test, run on the device, not installed as a module
#   test_wake_shim.py       device-side test, same
DEVICE_EXCLUSIONS = {
    "satellite.py",
    "test_led_controller.py",
    "test_wake_shim.py",
}

# The upstream audio-service examples that live on a device but are not ours to ship.
UPSTREAM_ONLY = {
    "4mic_service.py",
    "usbmic_service.py",
}


def device_modules() -> set[str]:
    """Every project module in pi/ that belongs on a device."""
    return {
        path.name
        for path in PI.glob("*.py")
        if path.name not in DEVICE_EXCLUSIONS
    }


def deploy_lists() -> list[set[str]]:
    """Every `for f in ...; do` module list in the deploy script."""
    text = SCRIPT.read_text()
    return [
        set(match.split())
        for match in re.findall(r"for f in ([^;]+);\s*do", text)
    ]


def main() -> int:
    failures: list[str] = []

    if not SCRIPT.is_file():
        print(f"  FAIL: {SCRIPT} not found")
        return 1

    expected = device_modules()
    lists = deploy_lists()

    if not lists:
        failures.append("no `for f in ...` list found in the deploy script")
    else:
        if len(lists) < 2:
            failures.append(
                f"expected both a copy loop and a verification loop, found {len(lists)}"
            )
        for index, found in enumerate(lists):
            missing = expected - found
            extra = found - expected
            if missing:
                failures.append(
                    f"list {index + 1} omits device module(s) that would never be deployed: "
                    f"{sorted(missing)}"
                )
            if extra:
                failures.append(
                    f"list {index + 1} names non-device file(s): {sorted(extra)}"
                )
        if len(lists) >= 2 and lists[0] != lists[1]:
            failures.append(
                "the copy list and the verification list disagree: "
                f"{sorted(lists[0])} vs {sorted(lists[1])}"
            )

    for name in sorted(expected):
        if not (PI / name).is_file():
            failures.append(f"expected device module missing from pi/: {name}")

    for name in sorted(UPSTREAM_ONLY):
        if name in expected:
            failures.append(f"upstream-only example counted as ours: {name}")

    if failures:
        for failure in failures:
            print("  FAIL:", failure)
        return 1

    print(
        f"OK: the deploy script ships all {len(expected)} device modules, "
        "and copies and verifies the same set"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
