#!/usr/bin/env python3
"""Offline installer product-contract regressions, included in the local gate."""
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for package in ('installer', 'gateway', 'device-esp32', 'device-pi'):
    sys.path.insert(0, str(ROOT / 'packages' / package / 'src'))
suite = unittest.TestLoader().discover(str(ROOT / 'packages/installer/tests'), pattern='test_*.py')
if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
    sys.exit(1)
