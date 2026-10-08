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
#       --tls-certs ~/hermes-voice-bridge/state/registry-tls --device-id <id> \
#       --endpoint tls://<host>:10702 \
#       --wifi-ssid <ssid> --wifi-psk-file <file>
#   scripts/install-esp32.sh --dry-run ...               # print every command, touch nothing
#
# Provisioning is what makes a flashed board a satellite: the TLS credential region (so it can
# authenticate to the registry), the wake model, the endpoint it dials, and the Wi-Fi profile it
# joins. Everything is staged to the board's NVS, so the boot auto-start dials on its own.
#
# Passphrases, device keys and certificate material never come through this script's argv into
# logs: --tls-certs, --model, --wifi-psk-file and the model download all take file PATHS, and the
# provisioning helpers read them.

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
MODEL_FILE=""
MODEL_URL=""
MODEL_NAME=""   # empty = the model file's stem
TLS_CERTS_DIR=""  # the bridge's state/registry-tls: client-ca.crt, <device>.crt/.key, server.crt
DEVICE_ID=""      # the registry identity this board is enrolled as
ENDPOINT=""       # what the board dials: tls://host:port (tcp:// for a trusted LAN)
REGISTRY_FILE=""  # the bridge's state/device-credentials.json; defaults beside the certs dir
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
        --tls-certs) TLS_CERTS_DIR="${2:-}"; shift ;;
        --device-id) DEVICE_ID="${2:-}"; shift ;;
        --endpoint) ENDPOINT="${2:-}"; shift ;;
        --registry)  REGISTRY_FILE="${2:-}"; shift ;;
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
# Provisioning is what turns a flashed board into a satellite: the TLS credential region (so the
# registry will accept it), the wake model, the endpoint it dials, and the Wi-Fi profile it joins.
# It is deliberately a SET, not a menu: a board missing any one of the four is a dead board, so an
# incomplete provision is refused rather than half-applied. Credentials move as file paths, never
# through argv: the cert directory, the model file and the Wi-Fi passphrase are read by the
# provisioning helpers, so nothing secret lands in a shell history or a log line.
if [ "$DO_PROVISION" = 1 ]; then
    echo "== provision ($PORT) =="
    [ -n "$TLS_CERTS_DIR" ] || die "--tls-certs is required (the bridge's state/registry-tls directory)"
    [ -n "$DEVICE_ID" ]     || die "--device-id is required (the registry identity this board is enrolled as)"
    [ -n "$ENDPOINT" ]      || die "--endpoint is required (tls://host:port; the board dials exactly this)"
    [ -n "$WIFI_SSID" ]     || die "--wifi-ssid is required (the network the board joins)"
    [ -n "$WIFI_PSK_FILE" ] || die "--wifi-psk-file is required with --wifi-ssid (the passphrase moves as a file)"
    # The registry file sits beside the certs directory in the bridge's state/; --registry overrides.
    [ -n "$REGISTRY_FILE" ] || REGISTRY_FILE="$(dirname "$TLS_CERTS_DIR")/device-credentials.json"
    [ "$DRY_RUN" = 1 ] || [ -d "$TLS_CERTS_DIR" ] || die "no TLS cert directory at $TLS_CERTS_DIR"
    [ "$DRY_RUN" = 1 ] || [ -f "$TLS_CERTS_DIR/client-ca.crt" ] || die "no client-ca.crt in $TLS_CERTS_DIR"
    [ "$DRY_RUN" = 1 ] || [ -f "$TLS_CERTS_DIR/$DEVICE_ID.crt" ] || die "no $DEVICE_ID.crt in $TLS_CERTS_DIR (is the device enrolled?)"
    [ "$DRY_RUN" = 1 ] || [ -f "$REGISTRY_FILE" ] || die "no registry file at $REGISTRY_FILE (the bridge's state/device-credentials.json)"
    [ "$DRY_RUN" = 1 ] || [ -f "$WIFI_PSK_FILE" ] || die "wifi psk file not found: $WIFI_PSK_FILE"

    # 1. The TLS credential region: the CA that signed the bridge, this device's certificate and
    #    key, the pinned fingerprint of the bridge's certificate, and the host:port to dial.
    #    The bridge's registry file cross-checks that the certificate being written is the one the
    #    bridge has on file for this identity, so a mix-up is refused before a byte is written.
    run python3 "$REPO_ROOT/scripts/provision-device-tls.py" \
        --certs "$TLS_CERTS_DIR" --device "$DEVICE_ID" \
        --host "$(printf '%s' "$ENDPOINT" | sed -e 's#^[a-z]*://##' -e 's#:.*$##')" \
        --port  "$(printf '%s' "$ENDPOINT" | sed -e 's#.*:##')" \
        --registry "$REGISTRY_FILE" \
        --partitions "$FIRMWARE_DIR/partitions.csv" \
        --port-device "$PORT" --write

    # 2. The wake model. --model is a local file the user already has; --download-model fetches
    #    whatever model they can get (e.g. another community microwakeword model) from its URL.
    #    The bytes land in a temp file the provisioning helper reads -- nothing model-related
    #    travels through argv. With neither, the bundled default is provisioned so a plain install
    #    just works; its attribution and license are in firmware/esp32/models/NOTICE.md.
    if [ -n "$MODEL_URL" ]; then
        MODEL_FILE="$(mktemp "${TMPDIR:-/tmp}/wy_model.XXXXXX.tflite")"
        echo "   fetching wake model: $MODEL_URL"
        [ "$DRY_RUN" = 1 ] || curl -fsSL -o "$MODEL_FILE" "$MODEL_URL" || die "model download failed: $MODEL_URL"
        echo "   saved to $MODEL_FILE"
    elif [ -z "$MODEL_FILE" ]; then
        MODEL_FILE="$REPO_ROOT/firmware/esp32/models/hey_hermes.tflite"
        echo "   using the bundled default wake model: $MODEL_FILE"
    fi
    # The detection name defaults to the model's file stem (hey_hermes.tflite -> hey_hermes),
    # which is exactly what the device reports as the wake word, matching the Pi (which
    # reports whatever model it loaded). --model-name overrides it.
    [ -n "$MODEL_NAME" ] || MODEL_NAME="$(basename "$MODEL_FILE" | sed 's/\.tflite$//')"
    [ "$DRY_RUN" = 1 ] || [ -f "$MODEL_FILE" ] || die "model file not found: $MODEL_FILE"
    run python3 "$REPO_ROOT/scripts/provision-device-model.py" --port "$PORT" \
        --model "$MODEL_FILE" --name "$MODEL_NAME"

    # 3. The endpoint the board dials. The scheme is the transport: tls:// is the registry
    #    standard and tcp:// a trusted-LAN rollback. A passphrase is never an argument.
    run python3 "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" set-endpoint "$ENDPOINT"

    # 4. The Wi-Fi profile the board joins across reboots. The passphrase moves as a file; the
    #    device reports its presence, never its value.
    run python3 "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" save-profile "$WIFI_SSID" "$WIFI_PSK_FILE"
    run python3 "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" list-profiles
    echo "   provisioned: TLS credential, wake model, endpoint, Wi-Fi. On reboot the board"
    echo "   associates, dials $ENDPOINT and registers as $DEVICE_ID."
fi

echo "install-esp32: done"