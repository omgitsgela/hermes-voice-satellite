#!/usr/bin/env python3
"""Deterministic checks for the host configuration plane (#6/#11)."""
from __future__ import annotations

import pathlib
import stat
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge.config_plane import (  # noqa: E402
    ConfigConflict,
    ConfigPlane,
    ConfigValidationError,
    DEVICE_SETTING_SCHEMA,
)


def main() -> int:
    problems: list[str] = []
    checks = 0

    def check(label: str, condition: bool, detail="") -> None:
        nonlocal checks
        checks += 1
        print(f"{'PASS' if condition else 'FAIL'} {label}")
        if not condition:
            problems.append(f"{label}: {detail}")

    with tempfile.TemporaryDirectory() as tmp:
        state = pathlib.Path(tmp) / "fleet-config.json"
        plane = ConfigPlane(state)

        schema = plane.schema()
        check("schema is versioned", schema["schema_version"] == 1, schema)
        check("schema exposes bounded desired-state fields",
              {"wake_word", "wake_threshold", "mic_gain", "playback_volume",
               "led_brightness", "locale", "update_channel"} <= set(DEVICE_SETTING_SCHEMA),
              schema)

        first = plane.set_desired("kitchen-1", {
            "wake_word": "hey_hermes",
            "wake_threshold": 0.35,
            "mic_gain": 78,
            "playback_volume": 55,
            "led_brightness": 40,
            "locale": "en-US",
            "update_channel": "stable",
        }, expected_generation=0)
        check("first write advances generation", first["generation"] == 1, first)
        check("state is persisted private", state.exists() and stat.S_IMODE(state.stat().st_mode) == 0o600,
              oct(stat.S_IMODE(state.stat().st_mode)) if state.exists() else "missing")

        try:
            plane.set_desired("kitchen-1", {"playback_volume": 56}, expected_generation=0)
            conflict = False
        except ConfigConflict:
            conflict = True
        check("stale writes are rejected", conflict)

        bad_cases = [
            ("unknown field", {"shell_command": "reboot"}),
            ("wrong type", {"mic_gain": "loud"}),
            ("out of range", {"wake_threshold": 1.5}),
            ("unsafe wake word", {"wake_word": "../../bad"}),
        ]
        for label, patch in bad_cases:
            try:
                plane.set_desired("kitchen-1", patch, expected_generation=1)
                rejected = False
            except ConfigValidationError:
                rejected = True
            check(label + " is rejected", rejected)

        report = plane.report(
            "kitchen-1", generation=1,
            # Deliberately 5 away, not 1: playback_volume carries a one-unit round-trip
            # tolerance (see below), so a 1-unit difference is converged by design and
            # would not test drift at all.
            actual={**first["desired"], "playback_volume": 50},
            health="healthy", error=None,
        )
        check("reported state records generation and health",
              report["reported_generation"] == 1 and report["health"] == "healthy", report)
        check("drift is explicit",
              report["converged"] is False
              and report["drift"] == {"playback_volume": {"desired": 55, "reported": 50}}, report)

        listed = plane.list_devices(connected_ids={"kitchen-1"})
        check("fleet inventory exposes connectivity", listed[0]["connected"] is True, listed)
        check("state survives restart", ConfigPlane(state).get_device("kitchen-1")["generation"] == 1)

        seeded = plane.ensure_device("study-1", metadata={"name": "Study", "area": "study"})
        check("new devices receive a complete default profile",
              seeded["generation"] == 1
              and set(seeded["desired"]) == set(DEVICE_SETTING_SCHEMA), seeded)
        check("non-secret device metadata is retained", seeded["metadata"]["area"] == "study", seeded)

        try:
            plane.set_desired("../escape", {"mic_gain": 5}, expected_generation=0)
            invalid_id = False
        except ConfigValidationError:
            invalid_id = True
        check("device IDs cannot escape their namespace", invalid_id)

        # playback_volume survives a user->raw->user round trip through an integer ALSA
        # percentage, which shifts by one for 49 of the 100 values. Without a tolerance
        # those settings report permanent drift and never converge, which teaches whoever
        # reads the fleet view to ignore drift.
        tol = ConfigPlane(pathlib.Path(tmp) / "tol.json")
        tol.set_desired("kitchen-1", {"playback_volume": 50}, expected_generation=0)

        rep = tol.report("kitchen-1", generation=1, actual={"playback_volume": 51},
                         health="healthy", error=None)
        check("a one-unit round-trip difference on playback_volume is not drift",
              rep["drift"] == {} and rep["converged"] is True, rep)

        rep = tol.report("kitchen-1", generation=1, actual={"playback_volume": 49},
                         health="healthy", error=None)
        check("and the difference counts in either direction",
              rep["drift"] == {} and rep["converged"] is True, rep)

        rep = tol.report("kitchen-1", generation=1, actual={"playback_volume": 55},
                         health="healthy", error=None)
        check("a real difference is still reported as drift",
              "playback_volume" in rep["drift"] and rep["converged"] is False, rep)

        # The tolerance is per setting: a setting without one must still match exactly, or
        # the tolerance would quietly spread to every integer in the schema.
        tol.set_desired("kitchen-1", {"mic_gain": 78}, expected_generation=1)
        rep = tol.report("kitchen-1", generation=1,
                         actual={"playback_volume": 50, "mic_gain": 79},
                         health="healthy", error=None)
        check("a setting without a tolerance still matches exactly",
              "mic_gain" in rep["drift"] and "playback_volume" not in rep["drift"], rep)

        # A never-reported setting is drift, not agreement - the tolerance must not make a
        # missing value look present.
        rep = tol.report("kitchen-1", generation=1, actual={}, health="healthy", error=None)
        check("an unreported setting is still drift despite the tolerance",
              "playback_volume" in rep["drift"], rep)

    if problems:
        print("\nFAILURES")
        for problem in problems:
            print(" -", problem)
        return 1
    print(f"\nconfig plane checks: {checks}/{checks} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
