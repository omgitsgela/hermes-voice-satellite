#!/usr/bin/env bash
# Install the voice GATEWAY on a host that already runs Hermes: the bridge virtualenv, the
# bridge.env, the registry TLS material, the systemd user unit, and the Hermes-side plugin +
# config keys.
#
# This is the host-side counterpart of scripts/install-esp32.sh / install-pi.sh. A gateway host
# becomes a gateway in five steps, each visible and idempotent:
#
#   1. venv     -- the bridge's own virtualenv (faster-whisper, piper, wyoming, ...).
#   2. env      -- bridge.env from the committed template, with the registry link values filled
#                  in so the bridge listens the moment the unit starts.
#   3. tls      -- the registry's CA + server certificate and the credential file, minted by
#                  scripts/issue-registry-tls.py (idempotent; an existing CA is never regenerated).
#   4. unit     -- the systemd USER unit + the registry drop-in, enabled and started.
#   5. hermes   -- the voice_satellite plugin installed + enabled and the voice-surface keys
#                  applied to the Hermes config, one pass, with a backup.
#
# Usage:
#   scripts/install-gateway.sh --base ~/hermes-voice-bridge --device-reachable-as 192.0.2.10 \
#       [--registry-port 10701] [--tls-port 10702] [--satellite-host 192.0.2.20:10700] \
#       [--skip-hermes] [--dry-run]
#
# --device-reachable-as is the address a device reaches THIS bridge on: it becomes the server
# certificate's SAN and the endpoint a device is provisioned with, so it must be the address the
# device can actually route to (LAN IP or a name it resolves), never 127.0.0.1 or 0.0.0.0.
# --satellite-host is the old shape (bridge dials the device) and defaults to loopback: it is set
# for deployments that still dial out, and the registry link is the standard path.
#
# Idempotence: a re-run reports what it already did and changes nothing. The Hermes config is
# backed up before the one edit, and the registry CA is reused, never regenerated, because a
# regenerated CA strands every enrolled device.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE=""                       # the clone of this repository the gateway runs from
REACHABLE_AS=""               # the address devices route to on this host
REGISTRY_PORT="10701"         # the registry listener (plaintext, trusted LAN)
TLS_PORT=""                   # defaults to REGISTRY_PORT+1; the TLS listener devices pin
SATELLITE_HOST="127.0.0.1:10700"   # the old shape: where the bridge dials the satellite
SKIP_HERMES=0
DRY_RUN=0
HERMES_BIN="hermes"

die() { echo "install-gateway: $*" >&2; exit 1; }

run() {
    if [ "$DRY_RUN" = 1 ]; then
        echo "DRY-RUN: $*"
    else
        echo "+ $*"
        "$@"
    fi
}

usage() {
    sed -n '2,35p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --base)                BASE="${2:-}"; shift ;;
        --device-reachable-as) REACHABLE_AS="${2:-}"; shift ;;
        --registry-port)       REGISTRY_PORT="${2:-}"; shift ;;
        --tls-port)            TLS_PORT="${2:-}"; shift ;;
        --satellite-host)      SATELLITE_HOST="${2:-}"; shift ;;
        --hermes-bin)          HERMES_BIN="${2:-}"; shift ;;
        --skip-hermes)         SKIP_HERMES=1 ;;
        --dry-run)             DRY_RUN=1 ;;
        -h|--help)             usage 0 ;;
        *)                     die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

[ -n "$BASE" ] || die "--base is required (the clone of this repository the gateway runs from)"
[ -n "$REACHABLE_AS" ] || die "--device-reachable-as is required (the address a device reaches this bridge on)"
case "$REACHABLE_AS" in
    127.0.0.1*|::1|localhost*|0.0.0.0*) die "--device-reachable-as must be an address a device can route to, not $REACHABLE_AS (loopback/any never receives a device's dial)" ;;
esac
[ "$DRY_RUN" = 1 ] || [ -d "$BASE/bridge" ] || die "no bridge/ under $BASE -- is --base the repository clone?"
[ -f "$BASE/bridge.env.example" ] || die "no bridge.env.example at $BASE"
[ -f "$BASE/requirements.txt" ] || die "no requirements.txt at $BASE"

TLS_PORT="${TLS_PORT:-$((REGISTRY_PORT + 1))}"
SATELLITE_HOST_ADDR="$(printf '%s' "$SATELLITE_HOST" | sed 's#:.*$##')"
SATELLITE_HOST_PORT="$(printf '%s' "$SATELLITE_HOST" | sed 's#.*:##')"
STATE_DIR="$BASE/state"
# The venv python, or a placeholder for a dry-run (which never needs the interpreter to exist).
PY="$BASE/.venv/bin/python"

# ---------------------------------------------------------------- 1. venv
echo "== 1. bridge virtualenv ($BASE/.venv) =="
if [ "$DRY_RUN" = 0 ] && [ -x "$BASE/.venv/bin/python" ] && \
   "$BASE/.venv/bin/python" -c 'import wyoming, faster_whisper' 2>/dev/null; then
    echo "   venv already has wyoming + faster-whisper; leaving it alone"
else
    run python3 -m venv "$BASE/.venv"
    run "$BASE/.venv/bin/python" -m pip install -r "$BASE/requirements.txt"
    run "$BASE/.venv/bin/python" -m pip check
    # The Piper voice the bridge speaks with (not committed; the unit's WorkingDirectory expects
    # voices/ next to bridge/).
    run "$BASE/.venv/bin/python" -m piper.download_voices --download-dir "$BASE/voices" en_US-hfc_male-medium
fi

# ---------------------------------------------------------------- 2. bridge.env
echo "== 2. bridge.env ($BASE/bridge.env) =="
ENV="$BASE/bridge.env"
if [ -f "$ENV" ]; then
    echo "   $ENV already exists; updating only the registry lines (a re-run must not clobber a tuned file)"
    if [ "$DRY_RUN" = 0 ]; then
        for key in VOICE_REGISTRY_ENABLED VOICE_REGISTRY_HOST VOICE_REGISTRY_PORT \
                   VOICE_REGISTRY_TLS_CERT VOICE_REGISTRY_TLS_KEY VOICE_REGISTRY_TLS_CLIENT_CA \
                   VOICE_REGISTRY_TLS_PORT VOICE_REGISTRY_CREDENTIALS; do
            grep -q "^${key}=" "$ENV" || echo "   (missing $key in the existing file; append it manually or delete $ENV and re-run)"
        done
    fi
else
    run cp "$BASE/bridge.env.example" "$ENV"
    if [ "$DRY_RUN" = 1 ]; then
        echo "DRY-RUN: set registry values in $ENV:"
        echo "  VOICE_REGISTRY_ENABLED=1"
        echo "  VOICE_REGISTRY_HOST=$REACHABLE_AS"
        echo "  VOICE_REGISTRY_PORT=$REGISTRY_PORT"
        echo "  VOICE_REGISTRY_TLS_CERT=$STATE_DIR/registry-tls/server.crt"
        echo "  VOICE_REGISTRY_TLS_KEY=$STATE_DIR/registry-tls/server.key"
        echo "  VOICE_REGISTRY_TLS_CLIENT_CA=$STATE_DIR/registry-tls/client-ca.crt"
        echo "  VOICE_REGISTRY_TLS_PORT=$TLS_PORT"
        echo "  VOICE_SATELLITE_HOST=$SATELLITE_HOST_ADDR"
        echo "  VOICE_SATELLITE_PORT=$SATELLITE_HOST_PORT"
    else
        {
            printf '\n# --- gateway installer: registry link (%s) -----------------------------\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            printf 'VOICE_REGISTRY_ENABLED=1\n'
            printf 'VOICE_REGISTRY_HOST=%s\n' "$REACHABLE_AS"
            printf 'VOICE_REGISTRY_PORT=%s\n' "$REGISTRY_PORT"
            printf 'VOICE_REGISTRY_TLS_CERT=%s\n' "$STATE_DIR/registry-tls/server.crt"
            printf 'VOICE_REGISTRY_TLS_KEY=%s\n' "$STATE_DIR/registry-tls/server.key"
            printf 'VOICE_REGISTRY_TLS_CLIENT_CA=%s\n' "$STATE_DIR/registry-tls/client-ca.crt"
            printf 'VOICE_REGISTRY_TLS_PORT=%s\n' "$TLS_PORT"
            printf 'VOICE_SATELLITE_HOST=%s\n' "$SATELLITE_HOST_ADDR"
            printf 'VOICE_SATELLITE_PORT=%s\n' "$SATELLITE_HOST_PORT"
        } >> "$ENV"
        echo "   appended the registry link + satellite dial values to $ENV"
    fi
fi

# ---------------------------------------------------------------- 3. registry TLS
echo "== 3. registry TLS material ($STATE_DIR/registry-tls) =="
if [ "$DRY_RUN" = 1 ]; then
    echo "DRY-RUN: $PY $REPO_ROOT/scripts/issue-registry-tls.py --state $STATE_DIR init --san $REACHABLE_AS"
    echo "DRY-RUN: $PY $REPO_ROOT/scripts/issue-registry-tls.py --state $STATE_DIR verify"
else
    "$PY" "$REPO_ROOT/scripts/issue-registry-tls.py" --state "$STATE_DIR" init --san "$REACHABLE_AS"
    "$PY" "$REPO_ROOT/scripts/issue-registry-tls.py" --state "$STATE_DIR" verify
    echo "   enroll each satellite with:"
    echo "     $PY $REPO_ROOT/scripts/issue-registry-tls.py --state $STATE_DIR enroll --new <device-id> \\"
    echo "         --endpoint-host $REACHABLE_AS --endpoint-port $TLS_PORT"
fi

# ---------------------------------------------------------------- 4. systemd user unit
echo "== 4. systemd user unit (hermes-voice-bridge.service) =="
UNIT_DIR="$HOME/.config/systemd/user"
if [ "$DRY_RUN" = 1 ]; then
    echo "DRY-RUN: install -m 644 $REPO_ROOT/hermes/systemd/hermes-voice-bridge.service $UNIT_DIR/"
    echo "DRY-RUN: systemctl --user daemon-reload && systemctl --user enable --now hermes-voice-bridge.service"
    echo "DRY-RUN: loginctl enable-linger \$USER (so the unit survives logout)"
else
    # The unit reads bridge.env as its EnvironmentFile, so the registry values written in step 2
    # are already visible to the service: no drop-in is needed (and a drop-in with the same values
    # would be a second place to edit, which is how configuration drifts).
    mkdir -p "$UNIT_DIR"
    install -m 644 "$REPO_ROOT/hermes/systemd/hermes-voice-bridge.service" "$UNIT_DIR/"
    systemctl --user daemon-reload
    systemctl --user enable --now hermes-voice-bridge.service
    loginctl enable-linger "$USER" 2>/dev/null || echo "   note: enable lingering manually (loginctl enable-linger $USER)"
    echo "   unit status:"
    systemctl --user --no-pager status hermes-voice-bridge.service | head -6
fi

# ---------------------------------------------------------------- 5. the Hermes side
if [ "$SKIP_HERMES" = 1 ]; then
    echo "== 5. Hermes side: SKIPPED (--skip-hermes) =="
else
    echo "== 5. Hermes side: plugin + config =="
    [ "$DRY_RUN" = 1 ] || command -v "$HERMES_BIN" >/dev/null || die "no $HERMES_BIN on this host (or pass --hermes-bin / --skip-hermes)"
    if [ "$DRY_RUN" = 1 ]; then
        echo "DRY-RUN: $HERMES_BIN plugins install $REPO_ROOT/hermes/plugins/voice_satellite"
        echo "DRY-RUN: $HERMES_BIN plugins enable voice_satellite"
        echo "DRY-RUN: apply the voice-surface keys to $HOME/.hermes/config.yaml (backup first)"
    else
        run "$HERMES_BIN" plugins install "$REPO_ROOT/hermes/plugins/voice_satellite"
        run "$HERMES_BIN" plugins enable voice_satellite
        CFG="$HOME/.hermes/config.yaml"
        if [ -f "$CFG" ]; then
            BACKUP="${CFG}.bak-$(date +%Y%m%d-%H%M%S)"
            cp "$CFG" "$BACKUP"
            echo "   backed up $CFG -> $BACKUP"
        fi
        run "$PY" "$REPO_ROOT/scripts/hermes-voice-config.py" \
            --config "$CFG" --example "$REPO_ROOT/hermes/config/voice-surface.example.yaml" \
            'plugins.enabled=["voice_satellite"]' \
            'plugins.entries.voice_satellite.allow_tool_override=false' \
            'gateway.api_server.host=127.0.0.1' \
            'gateway.api_server.port=8642'
        run "$HERMES_BIN" plugins enable voice_satellite
        echo "   the voice surface takes effect on the next gateway restart:"
        echo "     $HERMES_BIN gateway restart"
        # The remaining core patch (cron provider pinning) is unrelated to the voice surface and
        # tracked upstream; report its state without applying it, per hermes/patches/README.md.
        if [ -x "$REPO_ROOT/hermes/patches/check-patches.sh" ] && [ -d "$HOME/.hermes/hermes-agent" ]; then
            "$REPO_ROOT/hermes/patches/check-patches.sh" || echo "   note: a core patch is not applied (see hermes/patches/README.md)"
        fi
    fi
fi

echo
echo "install-gateway: done"
echo "  reachable as:  $REACHABLE_AS (registry :$REGISTRY_PORT, TLS :$TLS_PORT)"
echo "  satellite dial: $SATELLITE_HOST (old shape, for compatibility)"
echo "  to add a device:"
echo "    $REPO_ROOT/scripts/issue-registry-tls.py --state $STATE_DIR enroll --new <device-id> \\"
echo "        --endpoint-host $REACHABLE_AS --endpoint-port $TLS_PORT"
echo "    scripts/install-esp32.sh --provision --port <usb-port> --tls-certs $STATE_DIR/registry-tls \\"
echo "        --device-id <device-id> --endpoint tls://$REACHABLE_AS:$TLS_PORT --wifi-ssid <ssid> --wifi-psk-file <file>"
echo "    sudo scripts/install-pi.sh --base <device-base> --device-name <device-id> \\"
echo "        --endpoint tls://$REACHABLE_AS:$TLS_PORT --tls-certs $STATE_DIR/registry-tls"
