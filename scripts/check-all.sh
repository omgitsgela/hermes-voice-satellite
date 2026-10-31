#!/usr/bin/env bash
# The whole gate, on this machine. There is no CI: GitHub is this project's code host and issue
# tracker, not its build farm, so "it passes" has to mean it passed here.
#
#   scripts/check-all.sh            # suite + guard (a few minutes)
#   scripts/check-all.sh --build    # also build the firmware, when the ESP-IDF toolchain is sourced
#   scripts/check-all.sh --bundle   # also build the standalone installer and check its assets
#
# Every step is a real command with a real exit code; nothing is summarised into "ok" on faith.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO_ROOT="$(pwd)"

WITH_BUILD=0
WITH_BUNDLE=0
for flag in "$@"; do
    case "$flag" in
        --build)  WITH_BUILD=1 ;;
        --bundle) WITH_BUNDLE=1 ;;
        -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "check-all: unknown flag $flag" >&2; exit 2 ;;
    esac
done

failures=()
step() {
    local name="$1"; shift
    printf '\n=== %s\n' "$name"
    if "$@"; then
        printf '=== %s: OK\n' "$name"
    else
        printf '=== %s: FAILED\n' "$name"
        failures+=("$name")
    fi
}

# 1. The installer's own suites (core, devices, session/HTTP, pairing, e2e, cli, catalogue).
step "installer suite" bash scripts/test-installer.sh

# 2. The Gateway and device suites: every scripts/test_*.py, run with the checkout's interpreter.
step "gateway and device suites" bash -c '
    fail=0; scripts=0
    for t in scripts/test_*.py; do
        case "$t" in scripts/test-installer.sh) continue ;; esac
        scripts=$((scripts + 1))
        if ! .venv/bin/python -B "$t" >/tmp/check-all-one.log 2>&1; then
            echo "FAILED: $t"
            tail -20 /tmp/check-all-one.log
            fail=$((fail + 1))
        fi
    done
    echo "ran $scripts script(s), $fail failing"
    [ "$fail" -eq 0 ]
'

# 3. Nothing installation-specific in a public repository.
step "public-data guard" bash scripts/check-no-personal-data.sh

# 4. Optional: the firmware, when the toolchain is on PATH.
if [ "$WITH_BUILD" = 1 ]; then
    step "firmware build" bash -c 'cd firmware/esp32 && idf.py build >/tmp/check-all-fw.log 2>&1 || { tail -20 /tmp/check-all-fw.log; exit 1; }'
fi

# 5. Optional: the standalone installer, and the assets it must carry.
if [ "$WITH_BUNDLE" = 1 ]; then
    step "installer bundle" bash -c '
        bash scripts/build-installer.sh >/tmp/check-all-bundle.log 2>&1 || { tail -20 /tmp/check-all-bundle.log; exit 1; }
        env -i PATH=/usr/bin:/bin HOME=/tmp dist/hermes-voice-satellite/hermes-voice-satellite --check-assets
    '
fi

printf '\n'
if [ "${#failures[@]}" -gt 0 ]; then
    printf 'GATE FAILED: %s\n' "${failures[*]}"
    exit 1
fi
printf 'GATE PASSED (%s)\n' "$REPO_ROOT"
