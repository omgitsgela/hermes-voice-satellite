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
# TLS directory, existing Wi-Fi PSK files, and -- when a board is attached -- the device id the
# board itself reports), prompts for the rest with defaults shown in [brackets] (bare Enter
# accepts one), validates each answer with the same rules the flags use, prints a summary of
# exactly what it will do, and asks for an explicit yes/no before writing anything. A
# non-interactive stdin refuses the guided flow -- pass the flags for automation. --guided
# --dry-run previews the guided answers without writing.
#
# Plug-and-play enrolment: when the board has no certificates yet, the guided flow enrols it with
# the gateway using the five-word setup code the gateway printed at install (read from the saved
# file ~/.config/hermes-voice-satellite/enroll-token or typed at the prompt -- never in argv,
# never printed). The fetched certificates are kept under
# ~/.config/hermes-voice-satellite/certs. If the gateway cannot be reached, the flow falls back to
# asking for a certificate folder by hand. --dry-run never contacts the gateway.

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
DEVICE_ID=""      # the registry identity this board is enrolled as (its own MAC id)
DEVICE_NAME=""    # the friendly name the house knows it by (device_name in NVS)
ENDPOINT=""       # what the board dials: tls://host:port (tcp:// for a trusted LAN)
REGISTRY_FILE=""  # the bridge's state/device-credentials.json; defaults beside the certs dir
WIFI_SSID=""
WIFI_PSK_FILE=""
# The bundle sha256 the operator selected and reviewed. When set, the flash refuses any artifact
# that does not match it -- including a stale bundle that agrees with its own stale manifest, which
# is how an old image once got flashed while the source moved on.
EXPECTED_BUNDLE_SHA256=""
# Plug-and-play enrolment state (the guided flow only). The gateway's five-word setup code and the
# certificates it fetches live under the user's config dir; the code moves as a file path or a
# no-echo answer, never through argv and never into output.
ENROLL_DIR="$HOME/.config/hermes-voice-satellite/certs"
ENROLL_TOKEN_FILE="$HOME/.config/hermes-voice-satellite/enroll-token"
ENROL_MODE="folder"   # 'token' once a setup code is in play; 'folder' is the manual path
ENROL_TOKEN=""        # the typed setup code, held only until the exchange runs
ENROLL_STAGING=""     # fetched certificates, staged until the explicit yes moves them into place
# The interpreter that runs the provisioning helpers and esptool. Prefer the repository's own
# venv, which is where the docs say to work and where esptool + pyserial live; fall back to
# python3 so a machine that already has both is unchanged. Resolved once, used by every call.
PY="$REPO_ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
# Flags with no guided prompt of their own are collected verbatim here and forwarded to the
# re-executed run (guided_end): guided mode must run exactly what the user asked for, so
# nothing typed on the command line may be silently dropped.
PASSTHROUGH=()

die() { echo "install-esp32: $*" >&2; exit 1; }

# The shared helpers in lib/guided.sh are a plain function library and the NON-guided path uses
# them too (guided_normalise_endpoint fills in the scheme and port for --endpoint). Source them
# unconditionally so both paths behave the same; guided mode's own check below stays as the
# clearer error when the file is absent from a checkout.
if [ -f "$SCRIPT_DIR/lib/guided.sh" ]; then
    # shellcheck source=lib/guided.sh
    . "$SCRIPT_DIR/lib/guided.sh"
fi
command -v guided_normalise_endpoint >/dev/null 2>&1 ||
    die "this checkout is missing $SCRIPT_DIR/lib/guided.sh"
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
    sed -n '2,53p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# The gateway's setup code is five words joined by '-' (e.g. correct-horse-battery-staple-ferry):
# the operator's authority a brand-new board enrols with. Anything else -- a path, a sentence --
# is not a code, and the caller then treats the answer as a certificate folder instead.
guided_is_enroll_token() {
    local value="$1" rest="$1" segment count=0
    case "$value" in
        ''|*[!A-Za-z-]*) return 1 ;;
    esac
    while [ -n "$rest" ]; do
        segment="${rest%%-*}"
        case "$segment" in ''|*[!A-Za-z]*) return 1 ;; esac
        count=$((count + 1))
        case "$rest" in *-*) rest="${rest#*-}" ;; *) rest='' ;; esac
    done
    [ "$count" -eq 5 ]
}

# A value-taking flag must be followed by its value. Without the guard, a trailing `--port`
# (nothing after it) exits 1 with NO message at all: the arm's own shift empties the list and
# the loop's shift below dies under `set -e`. Say what is missing instead.
while [ $# -gt 0 ]; do
    case "$1" in
        --build)     DO_BUILD=1 ;;
        --flash)     DO_FLASH=1 ;;
        --provision) DO_PROVISION=1 ;;
        --guided)    GUIDED=1; GUIDED_EXPLICIT=1 ;;
        --dry-run)   DRY_RUN=1 ;;
        --port)      [ $# -ge 2 ] || die "$1 needs a value"; PORT="${2:-}"; shift ;;
        --bundle-dir) [ $# -ge 2 ] || die "$1 needs a value"; BUNDLE_DIR="${2:-}"; PASSTHROUGH+=(--bundle-dir "${2:-}"); shift ;;
        --expected-bundle-sha256) [ $# -ge 2 ] || die "$1 needs a value"; EXPECTED_BUNDLE_SHA256="${2:-}"; PASSTHROUGH+=(--expected-bundle-sha256 "${2:-}"); shift ;;
        --tls-certs) [ $# -ge 2 ] || die "$1 needs a value"; TLS_CERTS_DIR="${2:-}"; shift ;;
        --device-id) [ $# -ge 2 ] || die "$1 needs a value"; DEVICE_ID="${2:-}"; shift ;;
        --device-name) [ $# -ge 2 ] || die "$1 needs a value"; DEVICE_NAME="${2:-}"; shift ;;
        --endpoint)  [ $# -ge 2 ] || die "$1 needs a value"; ENDPOINT="${2:-}"; shift ;;
        --registry)  [ $# -ge 2 ] || die "$1 needs a value"; REGISTRY_FILE="${2:-}"; PASSTHROUGH+=(--registry "${2:-}"); shift ;;
        --model)     [ $# -ge 2 ] || die "$1 needs a value"; MODEL_FILE="${2:-}"; PASSTHROUGH+=(--model "${2:-}"); shift ;;
        --download-model) [ $# -ge 2 ] || die "$1 needs a value"; MODEL_URL="${2:-}"; PASSTHROUGH+=(--download-model "${2:-}"); shift ;;
        --model-name) [ $# -ge 2 ] || die "$1 needs a value"; MODEL_NAME="${2:-}"; PASSTHROUGH+=(--model-name "${2:-}"); shift ;;
        --wifi-ssid) [ $# -ge 2 ] || die "$1 needs a value"; WIFI_SSID="${2:-}"; shift ;;
        --wifi-psk-file) [ $# -ge 2 ] || die "$1 needs a value"; WIFI_PSK_FILE="${2:-}"; shift ;;
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

    echo "Setting up your voice satellite."
    echo
    echo "Two steps:"
    echo "   1. write the firmware to the satellite over USB"
    echo "   2. set it up: its name, the wake word, the network it joins, and how it reaches"
    echo "      your gateway"
    echo
    echo "Nothing is changed until you say yes at the end."
    echo
    LAN_IP="$(guided_detect_lan_ip)"
    SERIAL_PORTS="$(guided_detect_serial_ports)"
    echo "What I found:"
    if [ -n "$SERIAL_PORTS" ]; then
        echo "   satellite:    $(printf '%s' "$SERIAL_PORTS" | tr '\n' ' ')"
    else
        echo "   satellite:    not seen yet (plug it in, or type its port when asked)"
    fi
    if [ -f "$BUNDLE_DIR/flash.bundle.bin" ]; then
        echo "   firmware:     ready to write"
    else
        echo "   firmware:     not at $BUNDLE_DIR/flash.bundle.bin (download it first)"
    fi
    REGISTRY_TLS="$(guided_first_dir_with client-ca.crt \
        "$REPO_ROOT/state/registry-tls" \
        "$HOME/hermes-voice-bridge/state/registry-tls")"
    if [ -n "$REGISTRY_TLS" ]; then
        echo "   certificates: found at $REGISTRY_TLS"
    else
        echo "   certificates: not found (the device is enrolled on the gateway first)"
    fi
    WIFI_PSK="$(guided_first_file "$HOME/wifi-psk.txt" "$HOME/.wifi-psk" "$REPO_ROOT/wifi-psk.txt")"
    if [ -n "$WIFI_PSK" ]; then
        echo "   network:      a saved passphrase is on this machine"
    else
        echo "   network:      no saved passphrase yet (you can type one when asked)"
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
    guided_prompt_yn FLASH_YN "Write the firmware to the satellite over USB?" "$FLASH_YN"
    guided_prompt_yn PROV_YN "Set the satellite up (its name, the wake word, the network, and how it reaches your gateway)?" "$PROV_YN"

    if [ "$FLASH_YN" = y ] || [ "$PROV_YN" = y ]; then
        PORT_DEFAULT="${PORT:-$(printf '%s\n' "$SERIAL_PORTS" | head -n1)}"
        guided_prompt PORT "Satellite serial port" "${PORT_DEFAULT:-/dev/ttyACM0}"
    fi
    if [ "$PROV_YN" = y ]; then
        # The board derives its own identity from its factory MAC, so when one is attached, ASK
        # it: a read-only console query (stages nothing) whose answer becomes the prompt's
        # default instead of the placeholder. Any failure -- no board, no pyserial, an old
        # firmware that does not know the query -- is simply "no default" and the prompt runs
        # as before. The id is only ever a DEFAULT: the prompt still shows it and a typed answer
        # wins, because the registry identity is the operator's to confirm.
        DETECTED_DEVICE_ID=""
        if [ -z "$DEVICE_ID" ] && [ -n "$SERIAL_PORTS" ] && command -v "$PY" >/dev/null 2>&1; then
            DETECTED_DEVICE_ID="$("$PY" "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" device-id 2>/dev/null | head -n1 | tr -d '\r' || true)"
            if [ -z "$DETECTED_DEVICE_ID" ]; then
                # No console: a new board, or one still carrying a vendor image. The ROM bootloader
                # always answers, and the identity is derived from the same factory MAC the firmware
                # uses, so a first-time board can be prepared by this installer at all.
                echo "   (no satellite firmware answered; reading the board's factory identity instead)" >&2
                DETECTED_DEVICE_ID="$("$PY" "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" rom-mac 2>/dev/null | head -n1 | tr -d '\r' || true)"
            fi
            case "$DETECTED_DEVICE_ID" in
                ''|*[!A-Za-z0-9._-]*) DETECTED_DEVICE_ID="" ;;  # never trust a stray line as an id
            esac
            if [ -n "$DETECTED_DEVICE_ID" ]; then
                echo "   (read the satellite's own name: $DETECTED_DEVICE_ID)"
            fi
        fi
        # Certificates for the board's own id already on this machine: use them unchanged. This is
        # the second-run path -- nothing to type and nothing to fetch.
        KNOWN_CERTS=""
        if [ -n "$DETECTED_DEVICE_ID" ]; then
            KNOWN_CERTS="$(guided_first_dir_with "$DETECTED_DEVICE_ID.crt" \
                "$TLS_CERTS_DIR" "$ENROLL_DIR" \
                "$REPO_ROOT/state/registry-tls" \
                "$HOME/hermes-voice-bridge/state/registry-tls")"
            if [ -n "$KNOWN_CERTS" ] && { [ ! -f "$KNOWN_CERTS/client-ca.crt" ] || \
                    [ ! -f "$KNOWN_CERTS/$DETECTED_DEVICE_ID.key" ]; }; then
                KNOWN_CERTS=""   # a half set is not "certificates already here"
            fi
        fi
        if [ -n "$TLS_CERTS_DIR" ]; then
            # --tls-certs was given on the command line: the manual folder question, exactly as it
            # always was (a flag the user typed is never dropped).
            guided_prompt TLS_CERTS_DIR "Folder holding this satellite's certificates (client-ca.crt and the satellite's .crt/.key)" "$TLS_CERTS_DIR"
        elif [ -n "$KNOWN_CERTS" ]; then
            TLS_CERTS_DIR="$KNOWN_CERTS"
            echo "   (this satellite already has its certificates in $TLS_CERTS_DIR; reusing them)"
        else
            # First time for this board. Plain words, because "TLS directory" means nothing here:
            # the satellite needs its own certificate -- its personal pass to talk to your gateway --
            # and the five-word setup code the gateway printed at install is what fetches one.
            echo "   Before this satellite can talk to your gateway, it needs its own"
            echo "   certificate -- think of it as the satellite's personal pass, made just for"
            echo "   this satellite. A new satellite does not have one yet."
            echo
            echo "   The easy way: when your gateway was set up it printed a secret setup"
            echo "   code made of five words (something like correct-horse-battery-staple-ferry)."
            echo "   Type those five words here and I will fetch the satellite's pass for you --"
            echo "   nothing to find, nothing to copy."
            TOKEN_SAVED=""
            if [ -f "$ENROLL_TOKEN_FILE" ]; then
                TOKEN_SAVED=1
                echo
                echo "   (found your saved setup code in $ENROLL_TOKEN_FILE; Enter uses it)"
            fi
            SETUP_QUESTION="Setup code (five words), or the folder that already holds this satellite's certificates"
            while :; do
                printf '%s: ' "$SETUP_QUESTION"
                guided_read_line SETUP_ANSWER
                if [ -n "$SETUP_ANSWER" ] || [ -n "$TOKEN_SAVED" ]; then
                    break
                fi
                echo "   please type the five words (or the folder that already holds them)"
            done
        fi
        # Classify the answer above: five words is the gateway's setup code (fetch this board's
        # certificate), anything else is the manual certificate folder -- the pre-plug-and-play
        # path, unchanged. An empty answer uses the saved setup code when there is one.
        if [ -z "$KNOWN_CERTS" ]; then
            if [ -n "${SETUP_ANSWER:-}" ] && guided_is_enroll_token "$SETUP_ANSWER"; then
                ENROL_MODE="token"; ENROL_TOKEN="$SETUP_ANSWER"; TLS_CERTS_DIR="$ENROLL_DIR"
            elif [ -z "${SETUP_ANSWER:-}" ] && [ -n "${TOKEN_SAVED:-}" ]; then
                ENROL_MODE="token"; TLS_CERTS_DIR="$ENROLL_DIR"
            elif [ -z "$TLS_CERTS_DIR" ]; then
                ENROL_MODE="folder"; TLS_CERTS_DIR="${SETUP_ANSWER:-}"
            fi
        fi
        # Two different names, and conflating them is what broke a first install. The
        # REGISTRY IDENTITY is what the firmware claims at registration, and it is always the
        # id the board derives from its own factory MAC -- no console command overrides it.
        # The FRIENDLY NAME is what the house knows it by (device_name in NVS). So the typed
        # name is the friendly name, and the identity stays the board's own.
        if [ -n "$DETECTED_DEVICE_ID" ]; then
            DEVICE_ID="$DETECTED_DEVICE_ID"
            guided_prompt DEVICE_NAME "Name this satellite is known as" "${DEVICE_NAME:-}"
            if [ -n "$DEVICE_NAME" ] && [ "$DEVICE_NAME" != "$DEVICE_ID" ]; then
                echo "   (registered as $DEVICE_ID, known in the house as $DEVICE_NAME)"
            fi
        else
            # Nothing to read, so the typed name IS the identity and must match what the
            # board will claim. Say so rather than letting it fail at registration.
            guided_prompt DEVICE_ID "Name this satellite is enrolled under" "${DEVICE_ID:-esp32-1}"
            # A typed --device-name still wins: only fall back to the identity when no friendly
            # name was given at all.
            DEVICE_NAME="${DEVICE_NAME:-$DEVICE_ID}"
            echo "   (no board attached: this must match the id the board reports)"
        fi
        # Just the IP address or host name is enough: the scheme and the port are filled in,
        # and what was resolved is said out loud rather than happening silently.
        #
        # There is deliberately NO default. The obvious guess -- this machine's own LAN IP --
        # is the answer to a different question ("what is MY address?") and it is simply wrong
        # whenever the gateway is another machine, which is the usual case. A wrong default is
        # worse than no default, so this asks.
        #
        # The scheme is the extension point for other transports: tls:// and tcp:// work today,
        # and usb:// (a satellite wired straight into the gateway computer) or bt:// can be
        # added by widening the validator and the provisioning step, not by reshaping this.
        guided_prompt ENDPOINT "Your gateway (its IP address or host name)" "${ENDPOINT:-}" guided_valid_endpoint_strict
        ENDPOINT_BEFORE="$ENDPOINT"
        ENDPOINT="$(guided_normalise_endpoint "$ENDPOINT")"
        [ "$ENDPOINT" = "$ENDPOINT_BEFORE" ] || echo "   (using $ENDPOINT)"
        echo "    the satellite will reach your gateway at $ENDPOINT"
        guided_prompt WIFI_SSID "Wi-Fi network name the satellite joins" "${WIFI_SSID}"
        # The passphrase is TYPED here, never passed on a command line: argv is visible in `ps`,
        # in shell history and in logs. A typed answer goes into a private file (0600) that is
        # deleted again once the board holds it. An answer that is already a readable file is
        # taken as that file, so a pre-staged key keeps working, and --wifi-psk-file on the
        # command line wins over the prompt entirely.
        if [ -n "$WIFI_PSK_FILE" ]; then
            echo "   (using the passphrase file given on the command line: $WIFI_PSK_FILE)"
        else
            WIFI_PSK_TYPED=""
            guided_prompt_secret WIFI_PSK_TYPED "Wi-Fi passphrase (type it, or give the path to a file that holds it)"
            if [ -f "$WIFI_PSK_TYPED" ]; then
                WIFI_PSK_FILE="$WIFI_PSK_TYPED"
            else
                WIFI_PSK_FILE="${TMPDIR:-/tmp}/hermes-wifi-psk.$$.psk"
                if [ "$DRY_RUN" = 1 ]; then
                    echo "   (dry run: nothing is written; the passphrase would be held in a"
                    echo "    private file and deleted once the satellite has it)"
                else
                    ( umask 077; printf '%s\n' "$WIFI_PSK_TYPED" > "$WIFI_PSK_FILE" )
                    chmod 600 "$WIFI_PSK_FILE"
                fi
                # The guided flow re-execs itself, so a trap here is lost: carry the path through
                # the environment and let the provision step (or the abort hook) remove it once it
                # is no longer needed. Set even for a dry run, so the summary describes the run.
                export HERMES_DISPOSABLE_PSK="$WIFI_PSK_FILE"
            fi
            unset WIFI_PSK_TYPED
        fi

        # The exchange: only now are the board's name and the gateway's address known. A dry run
        # never contacts the gateway (and fetches nothing). A real run stages what it fetches and
        # moves it into place only after the explicit yes, so "nothing is written until then"
        # still holds even here.
        if [ "$ENROL_MODE" = token ]; then
            ENROLL_BODY="${ENDPOINT#*://}"
            ENROLL_GATEWAY="${VOICE_ENROLL_URL:-http://${ENROLL_BODY%%:*}:8765}"
            if [ "$DRY_RUN" = 1 ]; then
                echo "   (dry run: nothing fetched -- a real run would enrol $DEVICE_ID at"
                echo "    $ENROLL_GATEWAY and keep the satellite's certificates in $ENROLL_DIR)"
                TLS_CERTS_DIR="$ENROLL_DIR"
            else
                echo "   fetching this satellite's certificate from your gateway at $ENROLL_GATEWAY ..."
                ENROLL_STAGING="$(mktemp -d "${TMPDIR:-/tmp}/hermes-enrol.XXXXXX")"
                if [ -n "$ENROL_TOKEN" ]; then
                    # The typed setup code moves as a file path into the helper, never as an
                    # argument and never into output.
                    ENROLL_TOKEN_TMP="$(mktemp "${TMPDIR:-/tmp}/hermes-enrol-token.XXXXXX")"
                    (umask 077; printf '%s\n' "$ENROL_TOKEN" > "$ENROLL_TOKEN_TMP")
                else
                    ENROLL_TOKEN_TMP="$ENROLL_TOKEN_FILE"
                fi
                ENROL_OK=0
                ENROL_FORCE_ARGS=()
                ENROL_ATTEMPT=0
                while :; do
                    ENROL_ATTEMPT=$((ENROL_ATTEMPT + 1))
                    # Capture the status explicitly: a failed command inside `if` leaves $?
                    # pointing at the `if`, not at the command.
                    ENROL_STATUS=0
                    "$PY" "$REPO_ROOT/scripts/enroll_device.py" \
                        --device-id "$DEVICE_ID" --gateway "$ENROLL_GATEWAY" \
                        --endpoint "$ENDPOINT" --out "$ENROLL_STAGING" \
                        --token-file "$ENROLL_TOKEN_TMP" "${ENROL_FORCE_ARGS[@]}" || ENROL_STATUS=$?
                    [ "$ENROL_STATUS" = 0 ] && { ENROL_OK=1; break; }
                    # Exit 3 is the one refusal with a choice attached: this name already has a
                    # certificate, because an earlier attempt used it. Offer to reissue it rather
                    # than dropping into a certificate folder nobody has heard of.
                    if [ "$ENROL_STATUS" = 3 ] && [ "$ENROL_ATTEMPT" = 1 ]; then
                        echo
                        echo "   That name already has a certificate at the gateway -- an earlier"
                        echo "   attempt used it. I can issue a new one; the old one stops working."
                        guided_prompt_yn REISSUE_YN "Issue a new certificate for $DEVICE_ID" "Y"
                        if [ "$REISSUE_YN" = y ]; then
                            ENROL_FORCE_ARGS=(--force)
                            continue
                        fi
                    fi
                    break
                done
                if [ -n "$ENROL_TOKEN" ]; then rm -f "$ENROLL_TOKEN_TMP"; fi
                ENROL_TOKEN=""
                if [ "$ENROL_OK" = 1 ]; then
                    TLS_CERTS_DIR="$ENROLL_DIR"
                    echo "   your gateway issued this satellite's certificate."
                else
                    rm -rf "$ENROLL_STAGING"
                    ENROLL_STAGING=""
                    echo "   That did not work -- the reason is just above. The old way still"
                    echo "   works: if this machine already has the gateway's certificate files"
                    echo "   for this satellite, tell me which folder they are in."
                    # Optional, not required: pressing Enter here means "I do not have a folder",
                    # which is a real answer and must end the run cleanly rather than re-asking.
                    guided_prompt_optional TLS_CERTS_DIR "Folder holding this satellite's certificates (press Enter to stop here)" "$REGISTRY_TLS"
                    if [ -z "$TLS_CERTS_DIR" ]; then
                        echo
                        echo "   Stopping here. Nothing was written to the satellite."
                        echo "   To retry the certificate fetch, run this again and answer the setup-code"
                        echo "   question with the five words your gateway printed."
                        exit 1
                    fi
                    ENROL_MODE="folder"
                fi
            fi
        fi
    fi

    # The one question that is not about the board. It is asked last and labelled as such, so a
    # normal setup never has to think about compiling anything.
    guided_prompt_yn BUILD_YN "Compile the firmware yourself instead? (advanced; not needed)" "$BUILD_YN"
    if [ "$FLASH_YN" != y ] && [ "$PROV_YN" != y ] && [ "$BUILD_YN" != y ]; then
        die "nothing selected: write the firmware, set the satellite up, or both"
    fi

    echo
    echo "=== What I'll do ==="
    if [ "$BUILD_YN" = y ]; then echo "   build:   compile the firmware from source, then write it"; fi
    if [ "$FLASH_YN" = y ]; then echo "   write:   the firmware onto the satellite on $PORT"; fi
    if [ "$PROV_YN" = y ]; then
        echo "   set up:  its name, wake word, network, and link to your gateway (over $PORT)"
        if [ -n "$DEVICE_NAME" ] && [ "$DEVICE_NAME" != "$DEVICE_ID" ]; then
            echo "    device id:  $DEVICE_ID (known in the house as $DEVICE_NAME)"
        else
            echo "    device id:  $DEVICE_ID"
        fi
        echo "    endpoint:   $ENDPOINT"
        if [ "$ENROL_MODE" = token ]; then
            if [ "$DRY_RUN" = 1 ]; then
                echo "    tls certs:  $TLS_CERTS_DIR (a dry run: fetched with your setup code in a real run)"
            else
                echo "    tls certs:  $TLS_CERTS_DIR (fetched with your setup code)"
            fi
        else
            echo "    tls certs:  $TLS_CERTS_DIR"
        fi
        if [ -n "${HERMES_DISPOSABLE_PSK:-}" ]; then
            echo "    wi-fi:      $WIFI_SSID (the passphrase you typed; never printed, deleted once the satellite has it)"
        else
            echo "    wi-fi:      $WIFI_SSID (passphrase read from $WIFI_PSK_FILE, never printed)"
        fi
        if [ -n "$MODEL_URL" ]; then
            echo "    wake model: fetched from $MODEL_URL"
        elif [ -n "$MODEL_FILE" ]; then
            echo "    wake model: $MODEL_FILE"
        else
            echo "    wake model: the bundled default (hey_hermes.tflite)"
        fi
        if [ -n "$MODEL_NAME" ]; then echo "    model name: $MODEL_NAME (the name the device reports)"; fi
    fi
    GUIDED_ARGS=()
    if [ "$BUILD_YN" = y ]; then GUIDED_ARGS+=(--build); fi
    if [ "$FLASH_YN" = y ]; then GUIDED_ARGS+=(--flash); fi
    if [ "$PROV_YN" = y ]; then GUIDED_ARGS+=(--provision); fi
    if [ "$FLASH_YN" = y ] || [ "$PROV_YN" = y ]; then GUIDED_ARGS+=(--port "$PORT"); fi
    if [ "$PROV_YN" = y ]; then
        GUIDED_ARGS+=(--tls-certs "$TLS_CERTS_DIR" --device-id "$DEVICE_ID"
                      --device-name "${DEVICE_NAME:-$DEVICE_ID}" --endpoint "$ENDPOINT"
                      --wifi-ssid "$WIFI_SSID" --wifi-psk-file "$WIFI_PSK_FILE")
        if [ "$ENROL_MODE" = token ]; then
            # The fetch wrote the registry record beside the certificates; the flag path would
            # otherwise look for it one directory up (the bridge's own layout).
            GUIDED_ARGS+=(--registry "$ENROLL_DIR/device-credentials.json")
        fi
    fi
    # Forward the flags this flow has no prompt for, exactly as given: nothing the user typed
    # on the command line may be silently dropped.
    if [ "${#PASSTHROUGH[@]}" -gt 0 ]; then GUIDED_ARGS+=("${PASSTHROUGH[@]}"); fi
    if [ "$DRY_RUN" = 1 ]; then GUIDED_ARGS+=(--dry-run); fi
    # The fetch (if any) staged its files; the explicit yes is what moves them into their kept
    # place, and a refusal throws them away, so "nothing was written" is literally true either
    # way. Both hooks are no-ops without a staged fetch.
    enrol_commit() {
        [ -n "$ENROLL_STAGING" ] || return 0
        mkdir -p "$ENROLL_DIR"
        local staged
        for staged in "$ENROLL_STAGING"/*; do
            case "$staged" in
                */device-credentials.json) continue ;;   # merged below, never clobbered
            esac
            cp -f "$staged" "$ENROLL_DIR/"
        done
        if [ -f "$ENROLL_STAGING/device-credentials.json" ]; then
            # The registry file grows across boards: merge this board's entry into whatever is
            # already there instead of replacing it.
            "$PY" - "$ENROLL_STAGING/device-credentials.json" \
                "$ENROLL_DIR/device-credentials.json" <<'PYMERGE'
import json
import sys

new = json.load(open(sys.argv[1]))
record = {}
try:
    existing = json.load(open(sys.argv[2]))
    if isinstance(existing, dict):
        record = existing
except (OSError, ValueError):
    pass
record.update(new)
with open(sys.argv[2], 'w') as handle:
    json.dump(record, handle, indent=2)
    handle.write('\n')
PYMERGE
            chmod 600 "$ENROLL_DIR/device-credentials.json"
        fi
        rm -rf "$ENROLL_STAGING"
        ENROLL_STAGING=""
    }
    enrol_discard() {
        # Anything staged before the confirmation is thrown away on a refusal: the fetched
        # certificates, and the private file a typed Wi-Fi passphrase was held in.
        [ -n "${HERMES_DISPOSABLE_PSK:-}" ] && rm -f "$HERMES_DISPOSABLE_PSK"
        [ -n "$ENROLL_STAGING" ] || return 0
        rm -rf "$ENROLL_STAGING"
        ENROLL_STAGING=""
    }
    GUIDED_ON_APPROVE=enrol_commit
    GUIDED_ON_ABORT=enrol_discard
    guided_end install-esp32.sh "${GUIDED_ARGS[@]}"
fi

[ "$DO_BUILD" = 1 ] || [ "$DO_FLASH" = 1 ] || [ "$DO_PROVISION" = 1 ] || usage 1

# A board write or a provisioning step needs a port; --build alone does not.
if [ "$DO_FLASH" = 1 ] || [ "$DO_PROVISION" = 1 ]; then
    [ -n "$PORT" ] || die "--port is required with --flash or --provision"
    # A board write or a provisioning step drives esptool (and pyserial for the console). Check
    # the interpreter up front, so a missing dependency is one actionable line rather than a
    # ModuleNotFoundError in the middle of a flash.
    if [ "$DRY_RUN" = 0 ] && ! "$PY" -c 'import esptool, serial' 2>/dev/null; then
        die "'$PY' cannot import esptool and pyserial. Set up the repository venv first:
   python3 -m venv .venv && .venv/bin/pip install esptool pyserial"
    fi
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
    run "$PY" "$REPO_ROOT/scripts/bundle-esp32-flash.py" \
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
    # The integrity gate. TWO checks, and both are needed.
    #
    # 1. The bundle that is about to be written to 0x0 is hashed against the reviewed manifest's
    #    own bundle_sha256. This is the check that catches "the artifact I am flashing is not the
    #    artifact that was reviewed" -- the exact failure that cost a whole debugging session: the
    #    laptop kept an older bundle while the source kept moving, so several "the fix changed
    #    nothing" conclusions were drawn about code that had never been flashed. It runs on EVERY
    #    flash, build tree or not, because a source build being current says nothing about which
    #    file is in the flash directory.
    # 2. When a local build tree exists, re-hash the source components too, so a bundle that
    #    disagrees with the tree it was allegedly built from is also refused.
    FLASH_REGIONS_DIR="$BUNDLE_DIR/regions"
    [ "$DRY_RUN" = 1 ] || rm -rf "$FLASH_REGIONS_DIR"
    run "$PY" "$REPO_ROOT/scripts/bundle-esp32-flash.py" \
        --verify "$BUNDLE_DIR/flash.bundle.bin" --check "$BUNDLE_DIR/flash.manifest.txt" \
        ${EXPECTED_BUNDLE_SHA256:+--expected-sha256 "$EXPECTED_BUNDLE_SHA256"} \
        --extract "$FLASH_REGIONS_DIR"
    if [ -d "$FIRMWARE_DIR/build" ]; then
        run "$PY" "$REPO_ROOT/scripts/bundle-esp32-flash.py" \
            --build "$FIRMWARE_DIR/build" --check "$BUNDLE_DIR/flash.manifest.txt"
    fi
    # Write ONLY the regions the manifest names, each at its own offset.
    #
    # The merged bundle is one contiguous image with zero padding between its
    # components, so writing it whole from 0x0 ERASES every partition living in
    # those gaps -- on this board `nvs` (the device's settings) and `phy_init`
    # sit between the partition table and the app. That is why a firmware update
    # used to cost a full re-provision: the flash was wiping the settings itself.
    # This is not an ESP32 constraint; images that write only their own regions
    # keep the settings across updates. The bundle remains the published,
    # hash-verifiable artifact -- it is simply not written as one blob.
    FLASH_ARGS=()
    if [ -f "$BUNDLE_DIR/flash.manifest.txt" ]; then
        while read -r off name; do
            FLASH_ARGS+=("$off" "$FLASH_REGIONS_DIR/$(basename "$name")")
        done < <("$PY" -c '
import pathlib, sys
for raw in pathlib.Path(sys.argv[1]).read_text().splitlines():
    if raw.startswith("offset="):
        fields = dict(item.split("=", 1) for item in raw.split())
        print(fields["offset"], fields["name"])
' "$BUNDLE_DIR/flash.manifest.txt")
    fi
    if [ "${#FLASH_ARGS[@]}" -eq 0 ]; then
        # No manifest yet: a dry run may simply be previewing the flow before the
        # bundle has been downloaded. Say what the regions come from rather than
        # inventing offsets -- a guessed flash map is how a board gets bricked.
        [ "$DRY_RUN" = 1 ] || die "no flash regions in $BUNDLE_DIR/flash.manifest.txt"
        run "$PY" -m esptool --chip esp32s3 --port "$PORT" --baud 460800 \
            --before default-reset --after hard-reset \
            write_flash --flash_mode dio --flash_size 8MB --flash_freq 80m \
            "<offset and region taken from $BUNDLE_DIR/flash.manifest.txt>"
    else
        run "$PY" -m esptool --chip esp32s3 --port "$PORT" --baud 460800 \
            --before default-reset --after hard-reset \
            write_flash --flash_mode dio --flash_size 8MB --flash_freq 80m \
            "${FLASH_ARGS[@]}"
    fi
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
        *://*) die "--endpoint must use tls:// or tcp:// (got: $ENDPOINT)" ;;
        *)     : ;;   # a bare IP, host name, or host:port; filled in just below
    esac
    ENDPOINT="$(guided_normalise_endpoint "$ENDPOINT")"
    guided_endpoint_is_implemented "$ENDPOINT" ||
        die "--endpoint $ENDPOINT is a transport the firmware cannot carry yet; use tls://host:port or tcp://host:port"
    guided_valid_endpoint_strict "$ENDPOINT" || die "--endpoint is not usable: $ENDPOINT"
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
    run "$PY" "$REPO_ROOT/scripts/provision-device-tls.py" \
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
    run "$PY" "$REPO_ROOT/scripts/provision-device-model.py" --port "$PORT" \
        --model "$MODEL_FILE" --name "$MODEL_NAME"
    # The model blob on its own is not enough: the detector loads the model whose header name equals
    # the `wake_word` selector, and it keeps the PREVIOUS selector until told otherwise. Provisioning
    # a new model without it left the board discarding the new model and not waking at all.
    run "$PY" "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" set-wake "$MODEL_NAME"

    # 3. The endpoint the board dials. The scheme is the transport: tls:// is the registry
    #    standard and tcp:// a trusted-LAN rollback. A passphrase is never an argument.
    run "$PY" "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" set-endpoint "$ENDPOINT"
    # The friendly name is separate from the registry identity (which is the board's own MAC id
    # and cannot be changed). Set it when the house name differs, so describe/info show the name
    # the person chose instead of a MAC-derived id.
    if [ -n "$DEVICE_NAME" ] && [ "$DEVICE_NAME" != "$DEVICE_ID" ]; then
        run "$PY" "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" set-name "$DEVICE_NAME"
    fi

    # 4. The Wi-Fi profile the board joins across reboots. The passphrase moves as a file; the
    #    device reports its presence, never its value.
    run "$PY" "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" save-profile "$WIFI_SSID" "$WIFI_PSK_FILE"
    # The passphrase arrived through a private file this script wrote (see the guided flow). Now
    # that the board holds it in its own storage, that copy is removed. A file the operator
    # supplied is never touched.
    if [ -n "${HERMES_DISPOSABLE_PSK:-}" ] && [ "$HERMES_DISPOSABLE_PSK" = "$WIFI_PSK_FILE" ]; then
        rm -f "$WIFI_PSK_FILE"
        unset HERMES_DISPOSABLE_PSK
    fi
    run "$PY" "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" list-profiles
    # A first-time board only evaluates its register-on-boot gate once, before the provisioning
    # session's first command; a later save does not run it again. So the board is restarted here
    # explicitly, rather than telling the person the device is restarting when nothing asked it to.
    run "$PY" "$REPO_ROOT/scripts/esp32_provision.py" --port "$PORT" restart
    echo "   provisioned: TLS credential, wake model, endpoint, Wi-Fi."
    echo "   The board has been restarted. It should associate, dial $ENDPOINT and register as"
    echo "   $DEVICE_ID within about half a minute. If it does not, power-cycle it once and check"
    echo "   the Wi-Fi network it joined."
fi

echo "install-esp32: done"