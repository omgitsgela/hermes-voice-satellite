#!/usr/bin/env bash
# Run every offline installer check. No device, no gateway, no network.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
status=0
for suite in run_core_tests run_device_tests run_session_tests; do
    echo "== $suite"
    python3 "packages/installer/tests/$suite.py" 2>&1 | tail -3
    [ "${PIPESTATUS[0]}" -eq 0 ] || status=1
done
exit "$status"
