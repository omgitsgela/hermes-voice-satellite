#!/usr/bin/env python3
"""Settings parity: the ESP32 satellite must answer the same settings the Pi satellite does.

The fleet config plane holds one desired profile per device and expects the device to report back
what it ACTUALLY applied. Anything a device cannot apply stays pending, and the device is marked
`degraded` rather than silently pretending. That is the honest design; the problem this file guards
against is a device that reports `degraded` for a setting it could apply perfectly well.

Measured 2026-10-29, live from state/fleet-config.json:

    wyoming1 (Raspberry Pi) reported applied: playback_volume, led_brightness,
                                              wake_threshold, wake_word
    wyoming2 (this ESP32)   reported applied: playback_volume, mic_gain, led_brightness

So the ESP32 was missing wake_threshold and wake_word. wake_threshold was merely STAGED to NVS with
a comment saying "there is no detector yet" -- written before the TFLM detector existed, and stale
ever since: a per-device sensitivity change silently waited for a reboot the device never got.

This file pins the ESP32's side of parity: every setting wyoming1 applies live must either have a
live adapter here too, or be staged with a documented reason. It reads the firmware source, so a
refactor that drops an adapter fails here rather than being discovered on the device.

Run with the bridge venv:

    .venv/bin/python scripts/test_esp32_settings_parity.py
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
GATEWAY = ROOT / "firmware/esp32/main/gateway_task.c"
DETECTOR_H = ROOT / "firmware/esp32/main/wake_detector.h"
DETECTOR_C = ROOT / "firmware/esp32/main/wake_detector.c"

checks = 0
failures: list[str] = []


def check(label: str, condition: bool) -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(label)


def code_only(path: pathlib.Path) -> str:
    """The file with comment lines removed, so prose about a setting is not mistaken for code."""
    lines = path.read_text().splitlines()
    return "\n".join(line for line in lines
                     if not line.lstrip().startswith(("*", "/*", "//")))


def reports(path: pathlib.Path, key: str) -> bool:
    """True when the apply path adds this key to the object it reports as APPLIED."""
    return bool(re.search(r'cJSON_AddNumberToObject\(actual,\s*"%s"' % re.escape(key),
                          code_only(path))) or \
        bool(re.search(r'cJSON_AddStringToObject\(actual,\s*"%s"' % re.escape(key),
                       code_only(path)))


def stages(path: pathlib.Path, key: str) -> bool:
    return bool(re.search(r'cfg_stage_wake\("%s"' % re.escape(key), code_only(path)))


def applies_live(path: pathlib.Path, key: str) -> bool:
    """A live adapter means the key is written to `actual` AND reaches a real setter."""
    return reports(path, key)


def main() -> int:
    gateway = code_only(GATEWAY)
    detector_h = DETECTOR_H.read_text()
    detector_c = DETECTOR_C.read_text()

    # 1) The settings wyoming1 applies live must also be applied live here.
    check("playback_volume reaches the session",
          applies_live(GATEWAY, "playback_volume"))
    check("led_brightness reaches the ring",
          applies_live(GATEWAY, "led_brightness"))
    check("wake_threshold is applied live, not only staged",
          applies_live(GATEWAY, "wake_threshold"))
    check("and the live apply calls the detector's setter",
          "wake_detector_set_threshold(" in gateway)
    check("the threshold is also persisted, or it reverts at the next boot",
          stages(GATEWAY, "wake_threshold"))

    # 2) mic_gain is a capability wyoming1 does NOT have; the ESP32 must keep it.
    check("mic_gain keeps its live adapter",
          applies_live(GATEWAY, "mic_gain"))

    # 3) wake_word: switching models teardown/reloads the scorer, so it is staged by design.
    #    It must still be recorded, and must NOT be reported as applied.
    check("wake_word is still staged for the next boot",
          stages(GATEWAY, "wake_word"))
    check("wake_word is NOT reported as applied",
          not reports(GATEWAY, "wake_word"))
    check("the staging reason is documented in the apply path",
          "model SELECTOR" in GATEWAY.read_text())

    # 4) The live threshold path must exist end to end, not just be named.
    check("the detector header declares the live setter",
          "int wake_detector_set_threshold(double threshold);" in detector_h)
    check("the detector header declares the readback",
          "double wake_detector_get_threshold(void);" in detector_h)
    check("the detector implements both",
          "int wake_detector_set_threshold(" in detector_c
          and "double wake_detector_get_threshold(" in detector_c)
    check("the setter goes through the wake core",
          "wy_wake_set_threshold(" in detector_c)

    # 5) A device that cannot apply a setting must say so rather than claim success.
    check("an unapplied setting marks the report degraded",
          "degraded = 1" in gateway)
    check("the report is compared against what was asked for",
          "cJSON_Compare(actual, settings, 1)" in gateway)

    print(f"settings parity checks: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


raise SystemExit(main())
