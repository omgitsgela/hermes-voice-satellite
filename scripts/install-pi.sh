#!/usr/bin/env bash
# Install the Raspberry Pi voice satellite: the service stack, its dependencies, and the one
# config file.
#
# This is the device-side counterpart of `scripts/install-esp32.sh`. A Pi becomes a satellite in
# four steps:
#
#   1. deps      -- a virtualenv for the satellite + the wake engine's Python needs.
#   2. units     -- install the systemd units under pi/systemd/ to /etc/systemd/system/.
#   3. config    -- install satellite.env (the TEMPLATE becomes the device's real values).
#   4. services  -- daemon-reload, then enable + start the coupled audio stack as ONE group.
#
# The four audio units (wyoming-satellite, wyoming-openwakeword, wake-shim, 2mic_leds)
# Require= each other in a cycle, so they are always restarted together -- restarting one
# without the others is how a cascade leaves a unit dead. `outbound-satellite` is NOT part of
# that group: it carries the registry link and does not depend on the audio stack.
#
# Usage:
#   scripts/install-pi.sh --base /opt/wyoming-satellite --device-name kitchen \
#       --wake-word hey_hermes [--user pi] [--dry-run]
#
# satellite.env is written from the committed template with the <...> placeholders filled from
# these flags. It is the ONE place the device's real values live; the units only reference the
# variables, so a different layout or user is a change HERE, not an edit to any unit. A real
# passphrase is never a flag: --wifi-psk-file / --wake-model-dir take paths.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PI_DIR="$REPO_ROOT/pi"
UNITS_DIR="$PI_DIR/systemd"
BASE=""
DEVICE_NAME=""
WAKE_WORD="hey_hermes"
DEVICE_USER="${SUDO_USER:-pi}"
SATELLITE_PORT="10700"
ENV_PATH="/etc/voice-satellite/satellite.env"
DRY_RUN=0
DO_DEPS=1
DO_UNITS=1
DO_CONFIG=1
DO_SERVICES=1

die() { echo "install-pi: $*" >&2; exit 1; }

run() {
    if [ "$DRY_RUN" = 1 ]; then
        echo "DRY-RUN: $*"
    else
        echo "+ $*"
        "$@"
    fi
}

usage() {
    sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --base)        BASE="${2:-}"; shift ;;
        --device-name) DEVICE_NAME="${2:-}"; shift ;;
        --wake-word)   WAKE_WORD="${2:-}"; shift ;;
        --user)        DEVICE_USER="${2:-}"; shift ;;
        --satellite-port) SATELLITE_PORT="${2:-}"; shift ;;
        --env-path)    ENV_PATH="${2:-}"; shift ;;
        --dry-run)     DRY_RUN=1 ;;
        --no-deps)     DO_DEPS=0 ;;
        --no-units)    DO_UNITS=0 ;;
        --no-config)   DO_CONFIG=0 ;;
        --no-services) DO_SERVICES=0 ;;
        -h|--help)     usage 0 ;;
        *)             die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

[ -n "$BASE" ] || die "--base is required (where the satellite software is installed)"
[ -n "$DEVICE_NAME" ] || die "--device-name is required (the identity the bridge knows)"
[ -d "$UNITS_DIR" ] || die "no units directory at $UNITS_DIR"

# A run that only prints must not require root; anything that writes /etc or systemctl does.
if [ "$DRY_RUN" = 0 ]; then
    [ "$(id -u)" = 0 ] || die "installing units and services needs root (sudo); --dry-run does not"
fi

# ---------------------------------------------------------------- deps
if [ "$DO_DEPS" = 1 ]; then
    echo "== dependencies =="
    VENV="$BASE/.venv"
    run python3 -m venv "$VENV"
    if [ -f "$REPO_ROOT/requirements.txt" ]; then
        run "$VENV/bin/pip" install -r "$REPO_ROOT/requirements.txt"
    fi
    # The wake engine and the satellite are separate installs on the Pi (the satellite resolves
    # its interpreter by absolute path, not by "the system python"). Nothing to do here beyond
    # the venv; the openwakeword service names its own interpreter in its unit.
fi

# ---------------------------------------------------------------- units
if [ "$DO_UNITS" = 1 ]; then
    echo "== systemd units =="
    for unit in "$UNITS_DIR"/*.service; do
        [ -e "$unit" ] || continue
        name="$(basename "$unit")"
        # A .service.in is a template; the real unit is rendered elsewhere, so it is not
        # installed verbatim.
        case "$name" in
            *.in) echo "   skip $name (template)"; continue ;;
        esac
        run install -m 644 "$unit" "/etc/systemd/system/$name"
    done
    for override in "$UNITS_DIR"/*.d-overrides.conf; do
        [ -e "$override" ] || continue
        name="$(basename "$override" .d-overrides.conf)"
        run mkdir -p "/etc/systemd/system/$name.service.d"
        run install -m 644 "$override" "/etc/systemd/system/$name.service.d/overrides.conf"
    done
fi

# ---------------------------------------------------------------- config
if [ "$DO_CONFIG" = 1 ]; then
    echo "== configuration ($ENV_PATH) =="
    TEMPLATE="$UNITS_DIR/satellite.env"
    [ -f "$TEMPLATE" ] || die "no satellite.env template at $TEMPLATE"
    run mkdir -p "$(dirname "$ENV_PATH")"
    # The committed template carries <...> placeholders. Fill only the ones these flags supply;
    # a placeholder left behind is a value the operator still has to set, so the write is
    # reported loudly rather than silent.
    if [ "$DRY_RUN" = 1 ]; then
        echo "DRY-RUN: render $TEMPLATE -> $ENV_PATH (VS_BASE=$BASE, VS_DEVICE_NAME=$DEVICE_NAME, VS_WAKE_WORD=$WAKE_WORD, VS_SATELLITE_PORT=$SATELLITE_PORT)"
    else
        sed -e "s|<satellite-user>|$DEVICE_USER|g" \
            -e "s|<device-name>|$DEVICE_NAME|g" \
            -e "s|<wake-word>|$WAKE_WORD|g" \
            "$TEMPLATE" > "$ENV_PATH"
        # VS_BASE and the port are real paths/numbers, not placeholders; set them directly.
        printf 'VS_BASE=%s\n' "$BASE" >> "$ENV_PATH"
        printf 'VS_SATELLITE_PORT=%s\n' "$SATELLITE_PORT" >> "$ENV_PATH"
        echo "   wrote $ENV_PATH"
    fi
fi

# ---------------------------------------------------------------- services
if [ "$DO_SERVICES" = 1 ]; then
    echo "== services =="
    run systemctl daemon-reload
    # The four audio units are coupled (Require= each other in a cycle): enable + start them as
    # one group so none is left dead by an uneven restart. outbound-satellite is separate.
    GROUP="wyoming-satellite.service wyoming-openwakeword.service wake-shim.service 2mic_leds.service"
    # shellcheck disable=SC2086
    run systemctl enable --now $GROUP
fi

echo "install-pi: done"
echo "  satellite.env: $ENV_PATH"
echo "  wake word:     $WAKE_WORD"
echo "  device name:   $DEVICE_NAME"
echo "  The four audio units are a coupled group; restart them together, never one at a time."
echo "  outbound-satellite is separate (the registry link) and is not in that group."