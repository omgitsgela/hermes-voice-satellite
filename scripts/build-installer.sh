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
# Output: dist/hermes-voice-satellite/ (an executable plus its data). Ship the directory, or wrap it in a
# platform package (tar.gz, AppImage, MSI).
#
# Windows note: a venv keeps its binaries in Scripts/, not bin/, the tools carry .exe, and `python3`
# often does not exist. This script resolves all three, because the same file builds on every platform
# in CI and a build that only works on the author's machine is not a build.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

PYTHON="${PYTHON:-python3}"
VENV="${BUILD_VENV:-$ROOT/.build-venv}"

say() { printf '  %s\n' "$*" >&2; }

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    PYTHON="python"
fi
command -v "$PYTHON" >/dev/null 2>&1 || { say "no python on PATH"; exit 1; }
[ -f "$ROOT/packaging/hermes-voice-satellite.spec" ] || { say "packaging/hermes-voice-satellite.spec is missing"; exit 1; }

venv_bin() {
    if [ -x "$VENV/bin/python" ]; then printf '%s' "$VENV/bin"
    elif [ -x "$VENV/Scripts/python.exe" ]; then printf '%s' "$VENV/Scripts"
    fi
}

venv_python() {
    # Always run tools as `python -m <tool>`: on Windows pip refuses to upgrade pip.exe itself, and
    # the module form behaves the same on every platform.
    local dir; dir="$(venv_bin)"
    [ -n "$dir" ] || return 1
    for candidate in "$dir/python" "$dir/python.exe"; do
        [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
    done
    return 1
}

venv_tool() {
    # venv_tool pip -> the pip inside the venv, whatever this platform calls it
    local dir; dir="$(venv_bin)"
    [ -n "$dir" ] || return 1
    for candidate in "$dir/$1" "$dir/$1.exe"; do
        [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
    done
    return 1
}

if [ "$CHECK" -eq 1 ]; then
    say "python: $("$PYTHON" --version 2>&1)"
    say "spec:   packaging/hermes-voice-satellite.spec"
    if pyi="$(venv_tool pyinstaller)"; then
        say "pyinstaller: $("$pyi" --version) (in $VENV)"
    else
        say "pyinstaller: not installed yet; a build would create $VENV and install it"
    fi
    say "nothing was built"
    exit 0
fi

if [ -z "$(venv_bin)" ]; then
    say "creating $VENV"
    "$PYTHON" -m venv "$VENV"
fi

say "installing the packages and PyInstaller"
VENV_PY="$(venv_python)"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet pyinstaller
"$VENV_PY" -m pip install --quiet \
    "$ROOT/packages/gateway" \
    "$ROOT/packages/device-esp32" \
    "$ROOT/packages/device-pi" \
    "$ROOT/packages/installer"

say "building"
"$VENV_PY" -m PyInstaller --noconfirm \
    --distpath "$ROOT/dist" --workpath "$ROOT/build/pyinstaller" \
    "$ROOT/packaging/hermes-voice-satellite.spec" >&2

TARGET="$ROOT/dist/hermes-voice-satellite"
BINARY="$TARGET/hermes-voice-satellite"
[ -x "$BINARY" ] || BINARY="$TARGET/hermes-voice-satellite.exe"
if [ ! -x "$BINARY" ]; then
    say "the build finished but no executable is in $TARGET"
    exit 1
fi

say "built: $TARGET ($(du -sh "$TARGET" | cut -f1))"
say "check it runs with no Python:  $BINARY --status"
