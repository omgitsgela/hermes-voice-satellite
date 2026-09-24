#!/usr/bin/env python3
"""Repository regression check for issue #38.

The obsolete VAD drop-in must not be shipped as an executable configuration example: it contradicts
WakeStreamingSatellite behavior and resets the configured wake word if installed.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
obsolete = ROOT / "pi" / "wyoming-satellite-vad.conf"

if obsolete.exists():
    print(f"FAIL: obsolete executable VAD drop-in is still shipped: {obsolete.relative_to(ROOT)}")
    raise SystemExit(1)

print("dormant VAD configuration check: passed")
sys.exit(0)
