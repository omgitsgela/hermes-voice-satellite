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
#       --wake-word hey_hermes --endpoint tls://192.0.2.10:10702 \
#       --tls-certs /srv/bridge/state/registry-tls [--user pi] [--dry-run]
#   scripts/install-pi.sh                     # GUIDED setup: detect, prompt, confirm, then run
#   scripts/install-pi.sh --guided --dry-run  # the guided flow, previewing only
#
# --endpoint and --tls-certs configure the registry link that outbound-satellite carries. The
# endpoint's scheme is the transport (tls:// or tcp://) and its host:port become VOICE_BRIDGE_HOST
# / VOICE_BRIDGE_PORT; the TLS file set (client CA + this device's certificate and key) becomes the
# VOICE_BRIDGE_TLS_* paths the outbound satellite reads. Without them the device runs for local
# testing only and never reaches the registry.
#
# satellite.env is written from the committed template with the <...> placeholders filled from
# these flags. It is the ONE place the device's real values live; the units only reference the
# variables, so a different layout or user is a change HERE, not an edit to any unit.
#
# GUIDED mode: run with NO arguments (or --guided) and the script detects what it can (host LAN
# IP for the endpoint default, an existing registry TLS directory, an existing satellite.env),
# prompts for the rest with defaults shown in [brackets] (bare Enter accepts one), validates each
# answer with the same rules the flags use, prints a summary of exactly what it will do, and asks
# for an explicit yes/no before writing anything. A non-interactive stdin refuses the guided flow
# -- pass the flags for automation. --guided --dry-run previews the guided answers without writing.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
GUIDED=0
GUIDED_EXPLICIT=0
if [ $# -eq 0 ]; then GUIDED=1; fi
PI_DIR="$REPO_ROOT/pi"
UNITS_DIR="$PI_DIR/systemd"
BASE=""
DEVICE_NAME=""
WAKE_WORD="hey_hermes"
DEVICE_USER="${SUDO_USER:-pi}"
SATELLITE_PORT="10700"
ENDPOINT=""
TLS_CERTS_DIR=""
ENV_PATH="/etc/voice-satellite/satellite.env"
DRY_RUN=0
DO_DEPS=1
DO_UNITS=1
DO_CONFIG=1
DO_SERVICES=1
# Flags with no guided prompt of their own are collected verbatim here and forwarded to the
# re-executed run (guided_end): guided mode must run exactly what the user asked for, so
# nothing typed on the command line may be silently dropped.
PASSTHROUGH=()

die() { echo "install-pi: $*" >&2; exit 1; }

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

run() {
    if [ "$DRY_RUN" = 1 ]; then
        echo "DRY-RUN: $*"
    else
        echo "+ $*"
        "$@"
    fi
}

usage() {
    sed -n '2,41p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# A value-taking flag must be followed by its value. Without the guard, a trailing `--base`
# (nothing after it) exits 1 with NO message at all: the arm's own shift empties the list and
# the loop's shift below dies under `set -e`. Say what is missing instead.
while [ $# -gt 0 ]; do
    case "$1" in
        --base)        [ $# -ge 2 ] || die "$1 needs a value"; BASE="${2:-}"; shift ;;
        --device-name) [ $# -ge 2 ] || die "$1 needs a value"; DEVICE_NAME="${2:-}"; shift ;;
        --wake-word)   [ $# -ge 2 ] || die "$1 needs a value"; WAKE_WORD="${2:-}"; shift ;;
        --user)        [ $# -ge 2 ] || die "$1 needs a value"; DEVICE_USER="${2:-}"; shift ;;
        --satellite-port) [ $# -ge 2 ] || die "$1 needs a value"; SATELLITE_PORT="${2:-}"; shift ;;
        --endpoint)    [ $# -ge 2 ] || die "$1 needs a value"; ENDPOINT="${2:-}"; shift ;;
        --tls-certs)   [ $# -ge 2 ] || die "$1 needs a value"; TLS_CERTS_DIR="${2:-}"; shift ;;
        --env-path)    [ $# -ge 2 ] || die "$1 needs a value"; ENV_PATH="${2:-}"; shift ;;
        --guided)      GUIDED=1; GUIDED_EXPLICIT=1 ;;
        --dry-run)     DRY_RUN=1 ;;
        --no-deps)     DO_DEPS=0; PASSTHROUGH+=(--no-deps) ;;
        --no-units)    DO_UNITS=0; PASSTHROUGH+=(--no-units) ;;
        --no-config)   DO_CONFIG=0; PASSTHROUGH+=(--no-config) ;;
        --no-services) DO_SERVICES=0; PASSTHROUGH+=(--no-services) ;;
        -h|--help)     usage 0 ;;
        *)             die "unknown argument: $1 (see --help)" ;;
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
        guided_require_interactive install-pi.sh
    fi

    echo "Setting up your Raspberry Pi voice satellite."
    echo
    echo "I'll install what it needs to work, and connect it to your gateway."
    echo
    echo "Nothing is changed until you say yes at the end."
    echo
    echo "What I found:"
    LAN_IP="$(guided_detect_lan_ip)"
    if [ -n "$LAN_IP" ]; then
        echo "  host LAN IP:       $LAN_IP (default endpoint host)"
    else
        echo "  host LAN IP:       not detected (type the gateway endpoint by hand)"
    fi
    REGISTRY_TLS="$(guided_first_dir_with client-ca.crt \
        "$REPO_ROOT/state/registry-tls" \
        "$HOME/hermes-voice-bridge/state/registry-tls")"
    if [ -n "$REGISTRY_TLS" ]; then
        echo "  registry TLS dir:  $REGISTRY_TLS (has client-ca.crt; default for --tls-certs)"
    else
        echo "  registry TLS dir:  none found (enroll the device on the gateway first, or run unlinked)"
    fi
    if [ -f "$ENV_PATH" ]; then
        echo "  satellite.env:     $ENV_PATH exists (a re-run overwrites it from the template)"
    else
        echo "  satellite.env:     not written yet ($ENV_PATH)"
    fi
    if [ "$(id -u)" = 0 ]; then
        echo "  privileges:        running as root (a real install can proceed)"
    else
        echo "  privileges:        not root (a real install needs sudo; --dry-run does not)"
    fi
    echo

    guided_prompt BASE "Where the satellite software is installed" "${BASE:-/opt/wyoming-satellite}"
    guided_prompt DEVICE_NAME "Device name (the identity the bridge knows)" "${DEVICE_NAME:-$(hostname -s 2>/dev/null || echo satellite)}"
    guided_prompt WAKE_WORD "Wake word / detection name" "${WAKE_WORD:-hey_hermes}"
    guided_prompt DEVICE_USER "User the satellite services run as" "${DEVICE_USER:-${SUDO_USER:-pi}}"
    # Just the IP address or host name is enough: the scheme and port are filled in and the
    # resolved address is echoed back.
    guided_prompt_optional ENDPOINT "Your gateway (its IP address or host name)" "${ENDPOINT:-${LAN_IP:-}}" guided_valid_endpoint_loose
    if [ -n "$ENDPOINT" ]; then
        ENDPOINT_BEFORE="$ENDPOINT"
        ENDPOINT="$(guided_normalise_endpoint "$ENDPOINT")"
        [ "$ENDPOINT" = "$ENDPOINT_BEFORE" ] || echo "   (using $ENDPOINT)"
    fi
    if [ -n "$ENDPOINT" ]; then
        guided_prompt_optional TLS_CERTS_DIR "TLS certificates directory (client-ca.crt + this device's .crt/.key)" "${TLS_CERTS_DIR:-$REGISTRY_TLS}"
    fi
    guided_prompt ENV_PATH "satellite.env path" "${ENV_PATH:-/etc/voice-satellite/satellite.env}"
    guided_prompt SATELLITE_PORT "Satellite audio stream port" "${SATELLITE_PORT:-10700}" guided_valid_number

    echo
    echo "=== Summary: install-pi.sh will run exactly these steps ==="
    echo "  base:          $BASE (venv + the pi/ device modules)"
    echo "  device name:   $DEVICE_NAME (user $DEVICE_USER, wake word $WAKE_WORD)"
    echo "  satellite.env: $ENV_PATH"
    if [ -n "$ENDPOINT" ]; then
        echo "  registry link: $ENDPOINT (dials the gateway as $DEVICE_NAME)"
        if [ -n "$TLS_CERTS_DIR" ]; then
            echo "  TLS material:  $TLS_CERTS_DIR (client-ca + $DEVICE_NAME.crt/.key)"
        else
            echo "  TLS material:  none (plain link; fine on a trusted LAN only)"
        fi
    elif [ -n "$TLS_CERTS_DIR" ]; then
        # --tls-certs typed but the endpoint resolved empty: the flag path refuses this half
        # link (--tls-certs needs --endpoint), so say so here instead of advertising a local
        # test device the run will never be.
        echo "  registry link: none (the run refuses: --tls-certs $TLS_CERTS_DIR was given, but no endpoint)"
    else
        echo "  registry link: none (local test device; the outbound unit is not installed)"
    fi
    # The steps line lists only what this run will actually do: a skipped step is skipped in
    # the summary too, never advertised and then not run.
    STEPS_TEXT=""
    for step in "DO_DEPS dependencies" "DO_UNITS systemd units" "DO_CONFIG configuration" "DO_SERVICES services"; do
        var="${step%% *}"; label="${step#* }"
        if [ "${!var}" = 1 ]; then
            [ -z "$STEPS_TEXT" ] || STEPS_TEXT="$STEPS_TEXT -> "
            STEPS_TEXT="$STEPS_TEXT$label"
        fi
    done
    echo "  steps: ${STEPS_TEXT:-none (every step is skipped)}"
    echo "  note: the four audio units are a coupled group and are started together."
    GUIDED_ARGS=(--base "$BASE" --device-name "$DEVICE_NAME" --wake-word "$WAKE_WORD"
                 --user "$DEVICE_USER" --satellite-port "$SATELLITE_PORT" --env-path "$ENV_PATH")
    if [ -n "$ENDPOINT" ]; then
        GUIDED_ARGS+=(--endpoint "$ENDPOINT")
        if [ -n "$TLS_CERTS_DIR" ]; then GUIDED_ARGS+=(--tls-certs "$TLS_CERTS_DIR"); fi
    elif [ -n "$TLS_CERTS_DIR" ]; then
        # --tls-certs was typed but the endpoint resolved empty. Forward it verbatim anyway:
        # the re-executed run then hits the flag path's own refusal (--tls-certs needs
        # --endpoint) instead of guided silently dropping the flag and installing a local
        # test device. What the flag path refuses, guided refuses too -- nothing typed on the
        # command line is ever dropped.
        GUIDED_ARGS+=(--tls-certs "$TLS_CERTS_DIR")
    fi
    # Forward the flags this flow has no prompt for, exactly as given: nothing the user typed
    # on the command line may be silently dropped.
    if [ "${#PASSTHROUGH[@]}" -gt 0 ]; then GUIDED_ARGS+=("${PASSTHROUGH[@]}"); fi
    if [ "$DRY_RUN" = 1 ]; then GUIDED_ARGS+=(--dry-run); fi
    guided_end install-pi.sh "${GUIDED_ARGS[@]}"
fi

[ -n "$BASE" ] || die "--base is required (where the satellite software is installed)"
[ -n "$DEVICE_NAME" ] || die "--device-name is required (the identity the bridge knows)"
[ -d "$UNITS_DIR" ] || die "no units directory at $UNITS_DIR"
VENV="$BASE/.venv"

# The registry link: --tls-certs and --endpoint go together. A device enrolled in the registry
# needs both its credential files and the endpoint to dial; a device with neither is a local
# test device and the installer says so instead of silently wiring half a link.
ENDPOINT_HOST=""
ENDPOINT_PORT=""
if [ -n "$TLS_CERTS_DIR" ] && [ -z "$ENDPOINT" ]; then
    die "--tls-certs needs --endpoint (a device with credentials and nowhere to dial is a dead device)"
fi
if [ -n "$ENDPOINT" ] && [ -z "$TLS_CERTS_DIR" ]; then
    echo "  note: --endpoint without --tls-certs: the device will connect in the plain local mode"
    echo "        (fine on a trusted LAN; the registry will only accept it if it allows unauthenticated)"
fi
if [ -n "$ENDPOINT" ]; then
    ENDPOINT_HOST="$(printf '%s' "$ENDPOINT" | sed -e 's#^[a-z]*://##' -e 's#:.*$##')"
    ENDPOINT_PORT="$(printf '%s' "$ENDPOINT" | sed -e 's#.*:##')"
    case "$ENDPOINT_PORT" in ''|*[!0-9]*) die "endpoint port is not a number: $ENDPOINT" ;; esac
fi
if [ -n "$TLS_CERTS_DIR" ]; then
    [ "$DRY_RUN" = 1 ] || [ -d "$TLS_CERTS_DIR" ] || die "no TLS cert directory at $TLS_CERTS_DIR"
    [ "$DRY_RUN" = 1 ] || [ -f "$TLS_CERTS_DIR/client-ca.crt" ] || die "no client-ca.crt in $TLS_CERTS_DIR"
    [ "$DRY_RUN" = 1 ] || [ -f "$TLS_CERTS_DIR/$DEVICE_NAME.crt" ] || die "no $DEVICE_NAME.crt in $TLS_CERTS_DIR (is this device enrolled?)"
    [ "$DRY_RUN" = 1 ] || [ -f "$TLS_CERTS_DIR/$DEVICE_NAME.key" ] || die "no $DEVICE_NAME.key in $TLS_CERTS_DIR (is this device enrolled?)"
fi

# A run that only prints must not require root; anything that writes /etc or systemctl does.
if [ "$DRY_RUN" = 0 ]; then
    [ "$(id -u)" = 0 ] || die "installing units and services needs root (sudo); --dry-run does not"
fi

# ---------------------------------------------------------------- deps
if [ "$DO_DEPS" = 1 ]; then
    echo "== dependencies =="
    run python3 -m venv "$VENV"
    if [ -f "$REPO_ROOT/requirements.txt" ]; then
        run "$VENV/bin/pip" install -r "$REPO_ROOT/requirements.txt"
    fi
    # The runner and its siblings live in $BASE/examples/ -- the layout the audio units' ExecStart
    # lines expect (script/run is the upstream wyoming-satellite entry point; the units point at
    # $BASE/examples/*.py). install-pi copies them from this repo's pi/ tree so the rendered
    # outbound-satellite unit (whose ExecStart is $BASE/examples/outbound_satellite.py) can start.
    run mkdir -p "$BASE/examples"
    for module in outbound_satellite.py config_agent.py device_apply.py volume_control.py \
                  mic_gain_control.py wake_shim.py 2mic_service.py inject_wake.py led_cmd.py; do
        run install -m 644 "$REPO_ROOT/pi/$module" "$BASE/examples/$module"
    done
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
        run install -m 644 "$unit" "/etc/systemd/system/$name"
    done
    # outbound-satellite ships as a template because it carries three device-specific values:
    # the bootstrap file, the interpreter and the runner path. It is a device that talks to the
    # registry, so it is installed only for a registry-linked device; a local test device does not
    # get a link unit that can only fail.
    OUTBOUND_TEMPLATE="$UNITS_DIR/outbound-satellite.service.in"
    if [ -f "$OUTBOUND_TEMPLATE" ] && [ -n "$ENDPOINT" ]; then
        render() {
            sed -e "s|__ENV_FILE__|$ENV_PATH|g" \
                -e "s|__PYTHON__|$VENV/bin/python|g" \
                -e "s|__RUNNER__|$BASE/examples/outbound_satellite.py|g" \
                "$OUTBOUND_TEMPLATE"
        }
        if [ "$DRY_RUN" = 1 ]; then
            echo "DRY-RUN: install /etc/systemd/system/outbound-satellite.service (rendered from $OUTBOUND_TEMPLATE)"
        else
            render > /etc/systemd/system/outbound-satellite.service
            chmod 644 /etc/systemd/system/outbound-satellite.service
            echo "   installed outbound-satellite.service (registry link, env $ENV_PATH)"
        fi
    elif [ -f "$OUTBOUND_TEMPLATE" ]; then
        echo "   skip outbound-satellite (no --endpoint: this device has no registry link)"
    fi
    for override in "$UNITS_DIR"/*.d-overrides.conf; do
        [ -e "$override" ] || continue
        name="$(basename "$override" .d-overrides.conf)"
        # The override's stem is the unit's full name (wake-shim.service); the drop-in dir takes
        # the bare unit name.
        name="${name%.service}"
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
        if [ -n "$ENDPOINT" ]; then
            echo "DRY-RUN: append registry link:"
            echo "  VOICE_DEVICE_ID=$DEVICE_NAME"
            echo "  VOICE_BRIDGE_HOST=$ENDPOINT_HOST"
            echo "  VOICE_BRIDGE_PORT=$ENDPOINT_PORT"
            if [ -n "$TLS_CERTS_DIR" ]; then
                echo "  VOICE_BRIDGE_TLS_CA=$TLS_CERTS_DIR/client-ca.crt"
                echo "  VOICE_BRIDGE_TLS_CERT=$TLS_CERTS_DIR/$DEVICE_NAME.crt"
                echo "  VOICE_BRIDGE_TLS_KEY=$TLS_CERTS_DIR/$DEVICE_NAME.key"
            fi
        fi
    else
        sed -e "s|<satellite-user>|$DEVICE_USER|g" \
            -e "s|<device-name>|$DEVICE_NAME|g" \
            -e "s|<wake-word>|$WAKE_WORD|g" \
            "$TEMPLATE" > "$ENV_PATH"
        # VS_BASE and the port are real paths/numbers, not placeholders; set them directly.
        printf 'VS_BASE=%s\n' "$BASE" >> "$ENV_PATH"
        printf 'VS_SATELLITE_PORT=%s\n' "$SATELLITE_PORT" >> "$ENV_PATH"
        # The registry link that outbound-satellite reads. Absent (a local test device) and the
        # outbound unit simply does not run the link; present and the device dials the registry
        # as VS_DEVICE_NAME over the transport the endpoint's scheme names.
        if [ -n "$ENDPOINT" ]; then
            {
                echo ""
                echo "# Registry link (written by install-pi.sh; outbound-satellite reads it)."
                printf 'VOICE_DEVICE_ID=%s\n' "$DEVICE_NAME"
                printf 'VOICE_BRIDGE_HOST=%s\n' "$ENDPOINT_HOST"
                printf 'VOICE_BRIDGE_PORT=%s\n' "$ENDPOINT_PORT"
            } >> "$ENV_PATH"
            if [ -n "$TLS_CERTS_DIR" ]; then
                {
                    printf 'VOICE_BRIDGE_TLS_CA=%s\n' "$TLS_CERTS_DIR/client-ca.crt"
                    printf 'VOICE_BRIDGE_TLS_CERT=%s\n' "$TLS_CERTS_DIR/$DEVICE_NAME.crt"
                    printf 'VOICE_BRIDGE_TLS_KEY=%s\n' "$TLS_CERTS_DIR/$DEVICE_NAME.key"
                } >> "$ENV_PATH"
            fi
        fi
        echo "   wrote $ENV_PATH"
    fi
fi

# ---------------------------------------------------------------- services
if [ "$DO_SERVICES" = 1 ]; then
    echo "== services =="
    run systemctl daemon-reload
    # The four audio units are coupled (Require= each other in a cycle): enable + start them as
    # one group so none is left dead by an uneven restart. outbound-satellite is separate (the
    # registry link) and joins the group only when it was installed.
    GROUP="wyoming-satellite.service wyoming-openwakeword.service wake-shim.service 2mic_leds.service"
    [ -n "$ENDPOINT" ] && GROUP="$GROUP outbound-satellite.service"
    # shellcheck disable=SC2086
    run systemctl enable --now $GROUP
fi

echo "install-pi: done"
echo "  satellite.env: $ENV_PATH"
echo "  wake word:     $WAKE_WORD"
echo "  device name:   $DEVICE_NAME"
echo "  The four audio units are a coupled group; restart them together, never one at a time."
echo "  outbound-satellite is separate (the registry link) and is not in that group."