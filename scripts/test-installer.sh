#!/usr/bin/env bash
# Run every offline installer check. No device, no gateway, no network.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
status=0
for suite in run_core_tests run_device_tests run_session_tests \
              run_pairing_server_tests run_cli_tests run_catalogue_tests; do
    echo "== $suite"
    output=$(python3 "packages/installer/tests/$suite.py" 2>&1)
    code=$?
    # The summary, plus the names of anything that failed: a line count alone tells nobody what
    # broke, which is exactly what the first CI run demonstrated.
    printf '%s\n' "$output" | tail -3
    printf '%s\n' "$output" | grep -E "^(FAIL|ERROR):" | head -10
    [ "$code" -eq 0 ] || status=1
done
exit "$status"
