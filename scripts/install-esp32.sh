#!/usr/bin/env bash
# Install the ESP32/XVF3800 voice satellite on a board: flash, provision, (optional) build.
#
# This is the device-side counterpart of `scripts/install-pi.sh`. It turns a bare board into a
# working satellite. The default path needs NO ESP-IDF: flash the prebuilt bundle, then provision.
#
#   1. flash      -- `esptool` writes the PREBUILT bundle (bootloader, partition table, app) to
#                    0x0. This is the default: the published bundle is what a new user flashes.
#   2. provision  -- TLS material (out of band) + the wake model + Wi-Fi, over USB. This is what
#                    turns a flashed board into a satellite.
#   3. build      -- (opt-in) `idf.py build` + `bundle-esp32-flash.py`: compile the firmware from
#                    source and produce the bundle yourself. Needs ESP-IDF v6.1; a user who just
#                    wants a working board does not need it.
#
# Flashing is the one act with teeth, so it is never implied: --flash is required to write the
# board. The application region only: no chip erase, no eFuse or security change.
#
# Provisioning is a SET, not a menu: TLS credential + wake model + endpoint + Wi-Fi profile are
# all required, because a board missing any one is a dead board.
#
# Usage:
#   scripts/install-esp32.sh --flash --port /dev/ttyACM0   # flash the prebuilt bundle (no ESP-IDF)
#   scripts/install-esp32.sh --provision --port /dev/ttyACM0 \
#       --tls-certs ~/hermes-voice-bridge/state/registry-tls --device-id <id> \
#       --endpoint tls://<host>:10702 \
#       --wifi-ssid <ssid> --wifi-psk-file <file>
#   scripts/install-esp32.sh --build                       # (opt-in) build + bundle from source
#   scripts/install-esp32.sh --build --flash --port /dev/ttyACM0   # compile and flash in one go
#   scripts/install-esp32.sh --dry-run ...                 # print every command, touch nothing
#   scripts/install-esp32.sh                  # GUIDED setup: detect, prompt, confirm, then run
#   scripts/install-esp32.sh --guided --dry-run   # the guided flow, previewing only
#
# Provisioning stages to the board's NVS, so the boot auto-start dials on its own.
#
# Passphrases, device keys and certificate material never come through this script's argv into
# logs: --tls-certs, --model, --wifi-psk-file and the model download all take file PATHS, and the
# provisioning helpers read them.
#
# GUIDED mode: run with NO arguments (or --guided) and the script detects what it can (the USB
# serial ports /dev/ttyACM* and /dev/ttyUSB*, the prebuilt bundle, ESP-IDF, an existing registry
# TLS directory, existing Wi-Fi PSK files), prompts for the rest with defaults shown in [brackets]
# (bare Enter accepts one), validates each answer with the same rules the flags use, prints a
# summary of exactly what it will do, and asks for an explicit yes/no before writing anything. A
# non-interactive stdin refuses the guided flow -- pass the flags for automation. --guided
# --dry-run previews the guided answers without writing.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
GUIDED=0
GUIDED_EXPLICIT=0
if [ $# -eq 0 ]; then GUIDED=1; fi
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
    sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --build)     DO_BUILD=1 ;;
        --flash)     DO_FLASH=1 ;;
        --provision) DO_PROVISION=1 ;;
        --guided)    GUIDED=1; GUIDED_EXPLICIT=1 ;;
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

# ---------------------------------------------------------------- guided setup (no flags)
# Detection -> prompts (defaults in [brackets]) -> summary -> explicit yes/no -> the SAME steps
# the flag path runs (this script re-executes itself with the assembled flags).
if [ "$GUIDED" = 1 ]; then
    if [ -f "$SCRIPT_DIR/lib/guided.sh" ]; then
        # shellcheck source=lib/guided.sh
        . "$SCRIPT_DIR/lib/guided.sh"
    fi
    command -v guided_prompt >/dev/null 2>&1 ||
        die "guided setup needs $SCRIPT_DIR/lib/guided.sh, which is missing from this checkout"
    # No arguments at all needs a terminal; --guided may drive the prompts from a pipe.
    if [ "$GUIDED_EXPLICIT" = 0 ]; then
        guided_require_interactive install-esp32.sh
    fi

    echo "install-esp32: guided setup (no flags given)."
    echo "  This turns a USB-attached ESP32/XVF3800 board into a voice satellite:"
    echo "    - flash the prebuilt bundle (no ESP-IDF needed), and/or"
    echo "    - provision it (TLS credential, wake model, endpoint, Wi-Fi), and/or"
    echo "    - (opt-in) build the firmware from source with ESP-IDF."
    echo "  Nothing is written until you confirm at the end (--dry-run previews, never writes)."
    echo
    echo "Detection:"
    LAN_IP="$(guided_detect_lan_ip)"
    if [ -n "$LAN_IP" ]; then
        echo "  host LAN IP:       $LAN_IP (default endpoint host)"
    else
        echo "  host LAN IP:       not detected (type the gateway endpoint by hand)"
    fi
    SERIAL_PORTS="$(guided_detect_serial_ports)"
    if [ -n "$SERIAL_PORTS" ]; then
        echo "  USB serial ports:  $(printf '%s' "$SERIAL_PORTS" | tr '\n' ' ')"
    else
        echo "  USB serial ports:  none found (plug the board in, or type its port)"
    fi
    if [ -f "$BUNDLE_DIR/flash.bundle.bin" ]; then
        echo "  prebuilt bundle:   $BUNDLE_DIR/flash.bundle.bin (ready to flash)"
    else
        echo "  prebuilt bundle:   not at $BUNDLE_DIR/flash.bundle.bin (download it, or build from source)"
    fi
    if [ -f "$HOME/esp/idf-activate.sh" ]; then
        echo "  ESP-IDF:           found ($HOME/esp/idf-activate.sh; needed only to build)"
    else
        echo "  ESP-IDF:           not found (fine: only --build needs it)"
    fi
    REGISTRY_TLS="$(guided_first_dir_with client-ca.crt \
        "$REPO_ROOT/state/registry-tls" \
        "$HOME/hermes-voice-bridge/state/registry-tls")"
    if [ -n "$REGISTRY_TLS" ]; then
        echo "  registry TLS dir:  $REGISTRY_TLS (has client-ca.crt; default for --tls-certs)"
    else
        echo "  registry TLS dir:  none found (enroll the device on the gateway first)"
    fi
    WIFI_PSK="$(guided_first_file "$HOME/wifi-psk.txt" "$HOME/.wifi-psk" "$REPO_ROOT/wifi-psk.txt")"
    if [ -n "$WIFI_PSK" ]; then
        echo "  Wi-Fi PSK file:    $WIFI_PSK (found; default for --wifi-psk-file)"
    else
        echo "  Wi-Fi PSK file:    none found (the passphrase file must exist before provisioning)"
    fi
    echo

    # Defaults: flash + provision (what a new board needs); build stays opt-in. Explicit action
    # flags alongside --guided seed the defaults exactly instead.
    if [ "$DO_FLASH" = 1 ] || [ "$DO_PROVISION" = 1 ] || [ "$DO_BUILD" = 1 ]; then
        if [ "$DO_FLASH" = 1 ]; then FLASH_YN='y'; else FLASH_YN='n'; fi
        if [ "$DO_PROVISION" = 1 ]; then PROV_YN='y'; else PROV_YN='n'; fi
        if [ "$DO_BUILD" = 1 ]; then BUILD_YN='y'; else BUILD_YN='n'; fi
    else
        FLASH_YN='y'
        PROV_YN='y'
        BUILD_YN='n'
    fi
    guided_prompt_yn FLASH_YN "Flash the prebuilt bundle to the board over USB?" "$FLASH_YN"
    guided_prompt_yn PROV_YN "Provision the board (TLS credential, wake model, endpoint, Wi-Fi)?" "$PROV_YN"
    guided_prompt_yn BUILD_YN "Build the firmware from source? (needs ESP-IDF; opt-in)" "$BUILD_YN"
    if [ "$FLASH_YN" != y ] && [ "$PROV_YN" != y ] && [ "$BUILD_YN" != y ]; then
        die "nothing selected: pick at least one of flash, provision or build"
    fi

    if [ "$FLASH_YN" = y ] || [ "$PROV_YN" = y ]; then
        PORT_DEFAULT="${PORT:-$(printf '%s\n' "$SERIAL_PORTS" | head -n1)}"
        guided_prompt PORT "Board serial port" "${PORT_DEFAULT:-/dev/ttyACM0}"
    fi
    if [ "$PROV_YN" = y ]; then
        guided_prompt TLS_CERTS_DIR "TLS certificates directory (client-ca.crt + the device's .crt/.key)" "${TLS_CERTS_DIR:-$REGISTRY_TLS}"
        guided_prompt DEVICE_ID "Device id (the registry identity this board is enrolled as)" "${DEVICE_ID:-esp32-1}"
        guided_prompt ENDPOINT "Endpoint the board dials, tls://host:port or tcp://host:port" "${ENDPOINT:-${LAN_IP:+tls://$LAN_IP:10702}}" guided_valid_endpoint_strict
        guided_prompt WIFI_SSID "Wi-Fi network (SSID) the board joins" "${WIFI_SSID}"
        guided_prompt WIFI_PSK_FILE "Wi-Fi passphrase FILE (the passphrase never goes on the command line)" "${WIFI_PSK_FILE:-$WIFI_PSK}"
    fi

    echo
    echo "=== Summary: install-esp32.sh will run exactly these steps ==="
    if [ "$BUILD_YN" = y ]; then echo "  build:      idf.py build + bundle-esp32-flash.py -> $BUNDLE_DIR (needs ESP-IDF)"; fi
    if [ "$FLASH_YN" = y ]; then echo "  flash:      verify the bundle, then write_flash 0x0 on $PORT (application region only)"; fi
    if [ "$PROV_YN" = y ]; then
        echo "  provision:  TLS credential + wake model + endpoint + Wi-Fi, over $PORT"
        echo "    device id:  $DEVICE_ID"
        echo "    endpoint:   $ENDPOINT"
        echo "    tls certs:  $TLS_CERTS_DIR"
        echo "    wi-fi:      $WIFI_SSID (passphrase read from $WIFI_PSK_FILE, never printed)"
        echo "    wake model: the bundled default (hey_hermes.tflite)"
    fi
    GUIDED_ARGS=()
    if [ "$BUILD_YN" = y ]; then GUIDED_ARGS+=(--build); fi
    if [ "$FLASH_YN" = y ]; then GUIDED_ARGS+=(--flash); fi
    if [ "$PROV_YN" = y ]; then GUIDED_ARGS+=(--provision); fi
    if [ "$FLASH_YN" = y ] || [ "$PROV_YN" = y ]; then GUIDED_ARGS+=(--port "$PORT"); fi
    if [ "$PROV_YN" = y ]; then
        GUIDED_ARGS+=(--tls-certs "$TLS_CERTS_DIR" --device-id "$DEVICE_ID" --endpoint "$ENDPOINT"
                      --wifi-ssid "$WIFI_SSID" --wifi-psk-file "$WIFI_PSK_FILE")
    fi
    if [ "$DRY_RUN" = 1 ]; then GUIDED_ARGS+=(--dry-run); fi
    guided_end install-esp32.sh "${GUIDED_ARGS[@]}"
fi

[ "$DO_BUILD" = 1 ] || [ "$DO_FLASH" = 1 ] || [ "$DO_PROVISION" = 1 ] || usage 1

# A board write or a provisioning step needs a port; --build alone does not.
if [ "$DO_FLASH" = 1 ] || [ "$DO_PROVISION" = 1 ]; then
    [ -n "$PORT" ] || die "--port is required with --flash or --provision"
fi

# ---------------------------------------------------------------- build (opt-in)
# Compiling is the advanced path: it needs ESP-IDF v6.1 and the firmware source, and produces the
# bundle you would otherwise download. A user flashing the prebuilt bundle never reaches this block.
if [ "$DO_BUILD" = 1 ]; then
    [ "$DRY_RUN" = 1 ] || [ -d "$FIRMWARE_DIR" ] || die "no firmware directory at $FIRMWARE_DIR"
    [ "$DRY_RUN" = 1 ] || [ -f $HOME/esp/idf-activate.sh ] || die "ESP-IDF not found at $HOME/esp/idf-activate.sh (only --build needs it; to flash the prebuilt bundle, drop --build)"
    echo "== build (IDF_TARGET=$IDF_TARGET) =="
    run bash -c ". $HOME/esp/idf-activate.sh </dev/null >/dev/null 2>&1; export IDF_TARGET=$IDF_TARGET; cd '$FIRMWARE_DIR' && idf.py build"
    echo "== bundle (reviewable flash artifact + manifest) =="
    # A dry-run must not create directories either (it "touches nothing"), so the mkdir is
    # gated on the real run; the bundle step below is a no-op echo in dry-run anyway.
    [ "$DRY_RUN" = 1 ] || mkdir -p "$BUNDLE_DIR"
    run python3 "$REPO_ROOT/scripts/bundle-esp32-flash.py" \
        --build "$FIRMWARE_DIR/build" \
        --out "$BUNDLE_DIR/flash.manifest.txt" \
        --bundle "$BUNDLE_DIR/flash.bundle.bin"
    echo "   manifest: $BUNDLE_DIR/flash.manifest.txt"
    echo "   bundle:   $BUNDLE_DIR/flash.bundle.bin"
fi

# ---------------------------------------------------------------- flash
# The bundle carries bootloader + partition table + app and writes straight to 0x0. This is the
# only place anything is written to the board, and it is gated on --flash: never implied. The
# bundle is PREBUILT by default (the published image); --build produces one from source instead.
if [ "$DO_FLASH" = 1 ]; then
    echo "== flash ($PORT) =="
    [ "$DRY_RUN" = 1 ] || [ -f "$BUNDLE_DIR/flash.bundle.bin" ] || die "no bundle at $BUNDLE_DIR/flash.bundle.bin (download the prebuilt bundle, or pass --build to compile from source)"
    # The integrity gate. With a local source build present, re-hash every component against the
    # reviewed manifest. For a prebuilt bundle with no build tree, the manifest's own bundle_sha256
    # header is the gate: hash the one file that goes to 0x0.
    if [ -d "$FIRMWARE_DIR/build" ]; then
        run python3 "$REPO_ROOT/scripts/bundle-esp32-flash.py" \
            --build "$FIRMWARE_DIR/build" --check "$BUNDLE_DIR/flash.manifest.txt"
    else
        echo "   prebuilt bundle (no local build tree): verifying against the manifest's bundle hash"
        run python3 "$REPO_ROOT/scripts/bundle-esp32-flash.py" \
            --verify "$BUNDLE_DIR/flash.bundle.bin" --check "$BUNDLE_DIR/flash.manifest.txt"
    fi
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
    # Parse the endpoint into scheme / host / port and validate it before anything is written. An
    # unvalidated split (a host with no port, an IPv6 literal) produces a credential blob the
    # firmware or the blob parser rejects only later, with a worse message than a die() here.
    case "$ENDPOINT" in
        tls://*|tcp://*) ;;
        *) die "--endpoint must be tls://host:port or tcp://host:port (got: $ENDPOINT)" ;;
    esac
    ENDPOINT_BODY="${ENDPOINT#*://}"
    ENDPOINT_HOST="${ENDPOINT_BODY%%:*}"
    ENDPOINT_PORT="${ENDPOINT_BODY##*:}"
    [ "$ENDPOINT_HOST" != "$ENDPOINT_BODY" ] || die "--endpoint has no port: $ENDPOINT (want tls://host:port)"
    case "$ENDPOINT_HOST" in ''|*[![:alnum:].-]*) die "--endpoint host is not usable: $ENDPOINT_HOST" ;; esac
    case "$ENDPOINT_PORT" in ''|*[!0-9]*) die "--endpoint port is not a number: $ENDPOINT_PORT" ;; esac
    if [ "$ENDPOINT_PORT" -lt 1 ] || [ "$ENDPOINT_PORT" -gt 65535 ]; then
        die "--endpoint port out of range 1-65535: $ENDPOINT_PORT"
    fi
    # The registry file sits beside the certs directory in the bridge's state/; --registry overrides.
    [ -n "$REGISTRY_FILE" ] || REGISTRY_FILE="$(dirname "$TLS_CERTS_DIR")/device-credentials.json"
    [ "$DRY_RUN" = 1 ] || [ -d "$TLS_CERTS_DIR" ] || die "no TLS cert directory at $TLS_CERTS_DIR"
    [ "$DRY_RUN" = 1 ] || [ -f "$TLS_CERTS_DIR/client-ca.crt" ] || die "no client-ca.crt in $TLS_CERTS_DIR"
    [ "$DRY_RUN" = 1 ] || [ -f "$TLS_CERTS_DIR/$DEVICE_ID.crt" ] || die "no $DEVICE_ID.crt in $TLS_CERTS_DIR (is the device enrolled?)"
    [ "$DRY_RUN" = 1 ] || [ -f "$TLS_CERTS_DIR/$DEVICE_ID.key" ] || die "no $DEVICE_ID.key in $TLS_CERTS_DIR (is the device enrolled?)"
    [ "$DRY_RUN" = 1 ] || [ -f "$REGISTRY_FILE" ] || die "no registry file at $REGISTRY_FILE (the bridge's state/device-credentials.json)"
    [ "$DRY_RUN" = 1 ] || [ -f "$WIFI_PSK_FILE" ] || die "wifi psk file not found: $WIFI_PSK_FILE"

    # 1. The TLS credential region: the CA that signed the bridge, this device's certificate and
    #    key, the pinned fingerprint of the bridge's certificate, and the host:port to dial.
    #    The bridge's registry file cross-checks that the certificate being written is the one the
    #    bridge has on file for this identity, so a mix-up is refused before a byte is written.
    run python3 "$REPO_ROOT/scripts/provision-device-tls.py" \
        --certs "$TLS_CERTS_DIR" --device "$DEVICE_ID" \
        --host "$ENDPOINT_HOST" \
        --port  "$ENDPOINT_PORT" \
        --registry "$REGISTRY_FILE" \
        --partitions "$FIRMWARE_DIR/partitions.csv" \
        --port-device "$PORT" --write

    # 2. The wake model. --model is a local file the user already has; --download-model fetches
    #    whatever model they can get (e.g. another community microwakeword model) from its URL.
    #    The bytes land in a temp file the provisioning helper reads -- nothing model-related
    #    travels through argv. With neither, the bundled default is provisioned so a plain install
    #    just works; its attribution and license are in firmware/esp32/models/NOTICE.md.
    if [ -n "$MODEL_URL" ]; then
        if [ "$DRY_RUN" = 1 ]; then
            # A dry-run must not touch the filesystem, so it names the would-be temp path without
            # creating it; the name the device reports is still derived from the URL below.
            MODEL_FILE="${TMPDIR:-/tmp}/wy_model.tflite (dry-run: not created)"
        else
            MODEL_FILE="$(mktemp "${TMPDIR:-/tmp}/wy_model.XXXXXX.tflite")"
        fi
        echo "   fetching wake model: $MODEL_URL"
        [ "$DRY_RUN" = 1 ] || curl -fsSL -o "$MODEL_FILE" "$MODEL_URL" || die "model download failed: $MODEL_URL"
        echo "   saved to $MODEL_FILE"
    elif [ -z "$MODEL_FILE" ]; then
        MODEL_FILE="$REPO_ROOT/firmware/esp32/models/hey_hermes.tflite"
        echo "   using the bundled default wake model: $MODEL_FILE"
    fi
    # The detection name is what the device reports as the wake word. It defaults to the model's
    # file stem (hey_hermes.tflite -> hey_hermes), matching the Pi (which reports whatever model it
    # loaded). For --download-model the bytes land in a random temp file, so the name comes from the
    # URL's file name instead -- otherwise the device would report "wy_model.xK2pQ" as its wake word.
    # --model-name always wins.
    if [ -z "$MODEL_NAME" ]; then
        if [ -n "$MODEL_URL" ]; then
            MODEL_NAME="$(basename "${MODEL_URL%%\?*}" | sed 's/\.tflite$//')"
        else
            MODEL_NAME="$(basename "$MODEL_FILE" | sed 's/\.tflite$//')"
        fi
    fi
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