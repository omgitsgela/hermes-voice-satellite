#!/usr/bin/env bash
# Build the standalone installer for this platform.
#
# The result runs with no Python, no virtualenv and no pip on the target machine: CPython and every
# dependency are inside the bundle. That is the whole point, because asking a person to install a
# runtime before they can install anything is not an installer.
#
#   scripts/build-installer.sh            # build into dist/
#   scripts/build-installer.sh --check    # report what is missing and stop
#
# Output: dist/hermes-installer/ (a directory: an executable plus its data). Ship the directory, or
# wrap it in a platform package (tar.gz, AppImage, MSI). A one-file build is possible by setting
# HERMES_ONEFILE=1, at the cost of a slower first start, because the bundle is unpacked each run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

PYTHON="${PYTHON:-python3}"
VENV="${BUILD_VENV:-$ROOT/.build-venv}"

say() { printf '  %s\n' "$*" >&2; }

command -v "$PYTHON" >/dev/null 2>&1 || { say "no $PYTHON on PATH"; exit 1; }
[[ -f "$ROOT/packaging/hermes-installer.spec" ]] || { say "packaging/hermes-installer.spec is missing"; exit 1; }

if [ "$CHECK" -eq 1 ]; then
    say "python: $("$PYTHON" --version 2>&1)"
    say "spec:   packaging/hermes-installer.spec"
    if [ -x "$VENV/bin/pyinstaller" ]; then
        say "pyinstaller: $("$VENV/bin/pyinstaller" --version) (in $VENV)"
    else
        say "pyinstaller: not installed yet; a build would create $VENV and install it"
    fi
    say "nothing was built"
    exit 0
fi

if [ ! -x "$VENV/bin/python" ]; then
    say "creating $VENV"
    "$PYTHON" -m venv "$VENV"
fi

say "installing the packages and PyInstaller"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet pyinstaller
"$VENV/bin/pip" install --quiet \
    "$ROOT/packages/gateway" \
    "$ROOT/packages/device-esp32" \
    "$ROOT/packages/device-pi" \
    "$ROOT/packages/installer"

SPEC_ARGS=(--noconfirm --distpath "$ROOT/dist" --workpath "$ROOT/build/pyinstaller")
if [ "${HERMES_ONEFILE:-0}" = "1" ]; then
    say "one-file build requested: first start will be slower"
fi

say "building"
"$VENV/bin/pyinstaller" "${SPEC_ARGS[@]}" "$ROOT/packaging/hermes-installer.spec" >&2

TARGET="$ROOT/dist/hermes-installer"
[ -x "$TARGET/hermes-installer" ] || { say "the build finished but $TARGET/hermes-installer is missing"; exit 1; }

say "built: $TARGET ($(du -sh "$TARGET" | cut -f1))"
say "check it runs with no Python:  env -i PATH=/usr/bin:/bin $TARGET/hermes-installer --status >/dev/null"
