#!/usr/bin/env python3
"""Deterministic checks for the host configuration plane (#6/#11)."""
from __future__ import annotations

import pathlib
import stat
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gateway.config_plane import (  # noqa: E402
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
            "led_brightness": 31,
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

        # A report is a write like any other: acceptance means durable persistence.
        # Publishing the new reported state in memory before the save succeeds makes a
        # failed save look converged, and the fleet view then disagrees with the disk.
        import errno
        from unittest.mock import patch

        dur = ConfigPlane(pathlib.Path(tmp) / "durable.json")
        dur.ensure_device("kitchen-1")
        dur.report("kitchen-1", generation=1, actual={"playback_volume": 55},
                   health="healthy", error=None)
        before = dur.get_device("kitchen-1")
        disk_before = (pathlib.Path(tmp) / "durable.json").read_bytes()
        with patch.object(dur, "_save", side_effect=OSError(errno.ENOSPC, "disk full")):
            try:
                dur.report("kitchen-1", generation=1, actual={"playback_volume": 10},
                           health="error", error="simulated outage")
                rejected = False
            except OSError:
                rejected = True
        check("a report that cannot be persisted is reported as failed", rejected)
        after = dur.get_device("kitchen-1")
        check("a failed report does not publish health in memory",
              (after["health"], after["reported"]) == (before["health"], before["reported"]),
              f"before={before['health']}/{before['reported']} after={after['health']}/{after['reported']}")
        check("a failed report does not publish drift in memory",
              after["converged"] == before["converged"], after)
        check("a failed report leaves the file untouched",
              (pathlib.Path(tmp) / "durable.json").read_bytes() == disk_before)
        recovered = dur.report("kitchen-1", generation=1, actual={"playback_volume": 55},
                               health="healthy", error=None)
        check("the same report succeeds once storage recovers",
              recovered["health"] == "healthy"
              and recovered["reported"] == {"playback_volume": 55}
              and recovered["last_error"] is None, recovered)
        check("the recovered report is what a fresh reader sees",
              ConfigPlane(pathlib.Path(tmp) / "durable.json").get_device("kitchen-1")["health"]
              == "healthy")

        # -------------------------------------------------------------------
        # Legacy led_brightness migration (review F3)
        # -------------------------------------------------------------------
        # A device enrolled under the old schema carries led_brightness=40, a value the
        # 1-31 LED driver never had. Without migration, that poisoned value makes every
        # delivery for that device fail validation — even an unrelated volume change.
        # The migration repairs it on load, persisting the fix, and advances the
        # generation so the corrected profile re-delivers.
        import json as _json
        legacy_path = pathlib.Path(tmp) / "legacy.json"
        legacy_record = {
            "device_id": "kitchen-1",
            "metadata": {},
            "generation": 3,
            "desired": {
                "wake_word": "hey_hermes",
                "wake_threshold": 0.35,
                "mic_gain": 78,
                "playback_volume": 55,
                "led_brightness": 40,  # legacy out-of-range value
                "locale": "en-US",
                "update_channel": "stable",
            },
            "reported_generation": 3,
            "reported": {
                "wake_word": "hey_hermes",
                "wake_threshold": 0.35,
                "mic_gain": 78,
                "playback_volume": 55,
                "led_brightness": 40,
                "locale": "en-US",
                "update_channel": "stable",
            },
            "health": "healthy",
            "last_error": None,
            "desired_updated_at": "2026-09-20T10:00:00+00:00",
            "reported_at": "2026-09-20T10:00:01+00:00",
        }
        legacy_path.write_text(_json.dumps(
            {"schema_version": 1, "devices": {"kitchen-1": legacy_record}}
        ))
        migrated = ConfigPlane(legacy_path)
        rec = migrated.get_device("kitchen-1")
        check("legacy led_brightness=40 is migrated to 31 on load",
              rec["desired"]["led_brightness"] == 31, rec["desired"])
        check("the migration advances the generation",
              rec["generation"] == 4, rec["generation"])
        check("the migration is persisted (a fresh reader sees the fixed value)",
              ConfigPlane(legacy_path).get_device("kitchen-1")["desired"]["led_brightness"] == 31)
        # An unrelated volume write must succeed after migration (no longer poisoned).
        vol_write = migrated.set_desired("kitchen-1", {"playback_volume": 60},
                                         expected_generation=4)
        check("a volume write succeeds after the legacy migration",
              vol_write["generation"] == 5 and vol_write["desired"]["led_brightness"] == 31,
              vol_write)
        # A device already at 31 is untouched by the migration.
        in_range_path = pathlib.Path(tmp) / "in_range.json"
        in_range_record = dict(legacy_record)
        in_range_record["desired"] = dict(legacy_record["desired"])
        in_range_record["desired"]["led_brightness"] = 31
        in_range_record["reported"] = dict(legacy_record["reported"])
        in_range_record["reported"]["led_brightness"] = 31
        in_range_path.write_text(_json.dumps(
            {"schema_version": 1, "devices": {"kitchen-1": in_range_record}}
        ))
        untouched = ConfigPlane(in_range_path).get_device("kitchen-1")
        check("an in-range led_brightness is not touched by the migration",
              untouched["generation"] == 3 and untouched["desired"]["led_brightness"] == 31,
              untouched)

    if problems:
        print("\nFAILURES")
        for problem in problems:
            print(" -", problem)
        return 1
    print(f"\nconfig plane checks: {checks}/{checks} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
