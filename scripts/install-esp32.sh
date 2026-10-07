#!/usr/bin/env bash
# Install the ESP32/XVF3800 voice satellite on a board: build, bundle, flash, provision.
#
# This is the device-side counterpart of `scripts/install-pi.sh`. It takes the firmware that
# lives in `firmware/esp32/` and turns a bare board into a working satellite:
#
#   1. build      -- `idf.py build` for IDF_TARGET=esp32s3 (the application region).
#   2. bundle     -- `bundle-esp32-flash.py` builds the reviewable flash artifact + manifest.
#   3. flash      -- `esptool` writes the bundle (bootloader, partition table, app) to 0x0.
#   4. provision  -- TLS material (out of band) + the wake model + Wi-Fi, over USB.
#
# Flashing is the one act with teeth, so it is never implied: --flash is required to write the
# board, and without it this script builds + bundles + prints the exact flash command. The
# application region only: no chip erase, no eFuse or security change.
#
# Usage:
#   scripts/install-esp32.sh --build                     # build + bundle, print the flash step
#   scripts/install-esp32.sh --flash --port /dev/ttyACM0 # build + bundle + write the board
#   scripts/install-esp32.sh --provision --port /dev/ttyACM0 \
#       --tls-manifest <manifest.json> --model <hey_hermes.tflite> --model-name hey_hermes
#   scripts/install-esp32.sh --dry-run ...               # print every command, touch nothing
#
# Passphrases, device keys and certificate material never come through this script's argv into
# logs: --tls-manifest and --model take file PATHS, and the provisioning helpers read them.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRMWARE_DIR="$REPO_ROOT/firmware/esp32"
BUNDLE_DIR="${BUNDLE_DIR:-$REPO_ROOT/../xvf3800-flash}"
PORT=""
IDF_TARGET="esp32s3"
DO_BUILD=0
DO_FLASH=0
DO_PROVISION=0
DRY_RUN=0
TLS_MANIFEST=""
MODEL_FILE=""
MODEL_URL=""
MODEL_NAME=""   # empty = the model file's stem
WIFI_SSID=""
WIFI_PSK_FILE=""

die() { echo "install-esp32: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

run() {
    if [ "$DRY_RUN" = 1 ]; then
        echo "DRY-RUN: $*"
    else
        echo "+ $*"
        "$@"
    fi
}

# A sourced IDF export is a shell environment, not a command, so it is applied to THIS shell
# before the commands that need idf.py. The </dev/null keeps the export non-interactive; clearing
# PYTHONPATH keeps the agent's injected site-packages from shadowing the IDF venv.
with_idf() {
    # shellcheck disable=SC1091
    . $HOME/esp/idf-activate.sh </dev/null >/dev/null 2>&1
    export IDF_TARGET="$IDF_TARGET"
    "$@"
}

usage() {
    sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --build)     DO_BUILD=1 ;;
        --flash)     DO_FLASH=1 ;;
        --provision) DO_PROVISION=1 ;;
        --dry-run)   DRY_RUN=1 ;;
        --port)      PORT="${2:-}"; shift ;;
        --bundle-dir) BUNDLE_DIR="${2:-}"; shift ;;
        --tls-manifest) TLS_MANIFEST="${2:-}"; shift ;;
        --model)     MODEL_FILE="${2:-}"; shift ;;
        --download-model) MODEL_URL="${2:-}"; shift ;;
        --model-name) MODEL_NAME="${2:-}"; shift ;;
        --wifi-ssid) WIFI_SSID="${2:-}"; shift ;;
        --wifi-psk-file) WIFI_PSK_FILE="${2:-}"; shift ;;
        -h|--help)   usage 0 ;;
        *)           die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

[ "$DO_BUILD" = 1 ] || [ "$DO_FLASH" = 1 ] || [ "$DO_PROVISION" = 1 ] || usage 1

# A board write or a provisioning step needs a port; --build alone does not.
if [ "$DO_FLASH" = 1 ] || [ "$DO_PROVISION" = 1 ]; then
    [ -n "$PORT" ] || die "--port is required with --flash or --provision"
fi

# ---------------------------------------------------------------- build
if [ "$DO_BUILD" = 1 ] || [ "$DO_FLASH" = 1 ]; then
    [ "$DRY_RUN" = 1 ] || [ -d "$FIRMWARE_DIR" ] || die "no firmware directory at $FIRMWARE_DIR"
    [ "$DRY_RUN" = 1 ] || [ -f $HOME/esp/idf-activate.sh ] || die "ESP-IDF not found at $HOME/esp/idf-activate.sh"
    echo "== build (IDF_TARGET=$IDF_TARGET) =="
    run bash -c ". $HOME/esp/idf-activate.sh </dev/null >/dev/null 2>&1; export IDF_TARGET=$IDF_TARGET; cd '$FIRMWARE_DIR' && idf.py build"
fi

# ---------------------------------------------------------------- bundle
if [ "$DO_BUILD" = 1 ] || [ "$DO_FLASH" = 1 ]; then
    echo "== bundle (reviewable flash artifact + manifest) =="
    mkdir -p "$BUNDLE_DIR"
    run python3 "$REPO_ROOT/scripts/bundle-esp32-flash.py" \
        --build "$FIRMWARE_DIR/build" \
        --out "$BUNDLE_DIR/flash.manifest.txt" \
        --bundle "$BUNDLE_DIR/flash.bundle.bin"
    echo "   manifest: $BUNDLE_DIR/flash.manifest.txt"
    echo "   bundle:   $BUNDLE_DIR/flash.bundle.bin"
fi

# ---------------------------------------------------------------- flash
# The bundle carries bootloader + partition table + app and writes straight to 0x0. This is the
# only place anything is written to the board, and it is gated on --flash: never implied.
if [ "$DO_FLASH" = 1 ]; then
    echo "== flash ($PORT) =="
    [ "$DRY_RUN" = 1 ] || [ -f "$BUNDLE_DIR/flash.bundle.bin" ] || die "no bundle at $BUNDLE_DIR/flash.bundle.bin (run --build first)"
    run python3 "$REPO_ROOT/scripts/bundle-esp32-flash.py" \
        --build "$FIRMWARE_DIR/build" --check "$BUNDLE_DIR/flash.manifest.txt"
    run python3 -m esptool --chip esp32s3 --port "$PORT" --baud 460800 \
        --before default-reset --after hard-reset \
        write_flash --flash_mode dio --flash_size 8MB --flash_freq 80m \
        0x0 "$BUNDLE_DIR/flash.bundle.bin"
fi

# ---------------------------------------------------------------- provision
# Credentials move as file paths, never through argv: the TLS manifest and the model file are
# read by the provisioning helpers, so nothing secret lands in a shell history or a log line.
if [ "$DO_PROVISION" = 1 ]; then
    echo "== provision ($PORT) =="
    if [ -n "$TLS_MANIFEST" ]; then
        [ "$DRY_RUN" = 1 ] || [ -f "$TLS_MANIFEST" ] || die "TLS manifest not found: $TLS_MANIFEST"
        run python3 "$REPO_ROOT/scripts/provision-device-tls.py" --port "$PORT" --manifest "$TLS_MANIFEST"
    fi
    if [ -n "$MODEL_FILE" ]; then
        # The wake-model choice: --model is a local file the user already has; --download-model
        # fetches whatever model they can get (a community microwakeword model for testing, our
        # own hey_hermes later) from its URL. The bytes land in a temp file the provisioning
        # helper reads -- nothing model-related travels through argv.
        if [ -n "$MODEL_URL" ]; then
            MODEL_FILE="$(mktemp "${TMPDIR:-/tmp}/wy_model.XXXXXX.tflite")"
            echo "   fetching wake model: $MODEL_URL"
            [ "$DRY_RUN" = 1 ] || curl -fsSL -o "$MODEL_FILE" "$MODEL_URL" || die "model download failed: $MODEL_URL"
            echo "   saved to $MODEL_FILE"
        elif [ -z "$MODEL_FILE" ]; then
            # No custom model: use the bundled default so a plain install just works. Its
            # attribution and license are in firmware/esp32/models/NOTICE.md.
            MODEL_FILE="$REPO_ROOT/firmware/esp32/models/hey_jarvis.tflite"
            echo "   using the bundled default wake model: $MODEL_FILE"
        fi
        # The detection name defaults to the model's file stem (hey_jarvis.tflite -> hey_jarvis),
        # which is exactly what the device reports as the wake word, matching the Pi (which
        # reports whatever model it loaded). --model-name overrides it.
        [ -n "$MODEL_NAME" ] || MODEL_NAME="$(basename "$MODEL_FILE" | sed 's/\.tflite$//')"
        [ "$DRY_RUN" = 1 ] || [ -f "$MODEL_FILE" ] || die "model file not found: $MODEL_FILE"
        run python3 "$REPO_ROOT/scripts/provision-device-model.py" --port "$PORT" \
            --model "$MODEL_FILE" --name "$MODEL_NAME"
    fi
    if [ -n "$WIFI_SSID" ]; then
        [ -n "$WIFI_PSK_FILE" ] || die "--wifi-ssid needs --wifi-psk-file (the passphrase moves as a file)"
        [ "$DRY_RUN" = 1 ] || [ -f "$WIFI_PSK_FILE" ] || die "wifi psk file not found: $WIFI_PSK_FILE"
        run python3 "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" set-wifi-ssid "$WIFI_SSID"
        run python3 "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" set-wifi-psk "$WIFI_PSK_FILE"
    fi
    echo "   endpooint / Wi-Fi staging is a one-shot; run esp32_provision.py for the full console."
fi

echo "install-esp32: done"