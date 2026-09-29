#!/usr/bin/env python3
"""Deterministic checks for the fleet-wide desired-state write (#6).

`set_desired_fleet` is the "change it on every satellite" operation. Its properties, all
checked here without a running bridge:

* it bumps every targeted device's generation by exactly one and merges the patch into each
  one's desired state, in a single atomic save;
* it narrows to a named subset when `device_ids` is given and leaves the rest untouched;
* it refuses unknown targets and an empty patch rather than writing nothing silently;
* the restart-class classification (`setting_requires_restart`) is what the bridge uses to
  decide whether a fleet write must be paced, so it is pinned to the schema's `apply` field.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge.config_plane import (  # noqa: E402
    ConfigPlane,
    ConfigValidationError,
    DEVICE_SETTING_SCHEMA,
    setting_requires_restart,
)


def main() -> int:
    problems: list[str] = []
    checks = 0

    def check(label: str, condition: bool, detail="") -> None:
        nonlocal checks
        checks += 1
        if not condition:
            problems.append(f"{label}: {detail}")

    with tempfile.TemporaryDirectory() as tmp:
        state = pathlib.Path(tmp) / "fleet-config.json"
        plane = ConfigPlane(state)
        plane.ensure_device("kitchen-1")
        plane.ensure_device("study-1")
        plane.ensure_device("hall-1")

        # A fleet write touches every device, once each.
        records = plane.set_desired_fleet({"mic_gain": 80})
        check("a fleet write returns every device", len(records) == 3, len(records))
        by_id = {r["device_id"]: r for r in records}
        check("every device's generation advanced by one",
              all(r["generation"] == 2 for r in records), records)
        check("the patch is merged into each device's desired state",
              all(r["desired"].get("mic_gain") == 80 for r in records), records)
        check("untouched settings are preserved per device",
              by_id["kitchen-1"]["desired"].get("playback_volume") == 55,
              by_id["kitchen-1"]["desired"])

        # The write is durable and atomic: one save, and a fresh reader sees the same state.
        check("the fleet write is durable",
              ConfigPlane(state).get_device("study-1")["generation"] == 2)

        # A subset write narrows the target set and leaves the rest alone.
        subset = plane.set_desired_fleet({"playback_volume": 42},
                                         device_ids=["kitchen-1", "hall-1"])
        check("a subset write targets only the named devices",
              {r["device_id"] for r in subset} == {"kitchen-1", "hall-1"}, subset)
        study = plane.get_device("study-1")
        check("a device outside the subset is untouched",
              study["desired"].get("playback_volume") == 55
              and study["generation"] == 2, study)

        # Unknown and empty targets are refused, not silently ignored.
        for label, args, kwargs in [
            ("an unknown target is refused", ({"mic_gain": 5},), {"device_ids": ["ghost-1"]}),
            ("an empty target set is refused", ({"mic_gain": 5},), {"device_ids": []}),
            ("an empty patch is refused", ({},), {}),
        ]:
            try:
                plane.set_desired_fleet(*args, **kwargs)
                refused = False
            except ConfigValidationError:
                refused = True
            check(label, refused)

        # The restart-class classification is what the bridge paces on. It must track the
        # schema's `apply` field: a setting is restart-class exactly when applying it bounces
        # a device service.
        for name, spec in DEVICE_SETTING_SCHEMA.items():
            is_restart = setting_requires_restart(name)
            expected = spec["apply"] not in {"immediate", "next_turn", "next_update_check"}
            check(f"restart classification is correct for {name} ({spec['apply']})",
                  is_restart == expected, f"{name}: {is_restart} != {expected}")
        # The specific settings the device adapter treats as restart-class.
        check("wake_threshold is restart-class", setting_requires_restart("wake_threshold"))
        check("wake_word is restart-class", setting_requires_restart("wake_word"))
        check("led_brightness is restart-class", setting_requires_restart("led_brightness"))
        check("mic_gain is not restart-class", not setting_requires_restart("mic_gain"))
        check("playback_volume is not restart-class",
              not setting_requires_restart("playback_volume"))

        # The corrected LED range: 1-31, and the old 40 (and 0) are both rejected.
        for bad in (0, 32, 40):
            try:
                plane.set_desired("kitchen-1", {"led_brightness": bad},
                                  expected_generation=plane.get_device("kitchen-1")["generation"])
                rejected = False
            except ConfigValidationError:
                rejected = True
            check(f"led_brightness {bad} is out of range and rejected", rejected)

    if problems:
        print("\nFAILURES")
        for problem in problems:
            print(" -", problem)
        return 1
    print(f"\nfleet config checks: {checks}/{checks} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
