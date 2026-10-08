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
#       [--registry-port 10701] [--tls-port 10702] [--registry-bind 0.0.0.0] \
#       [--satellite-host 192.0.2.20:10700] [--skip-hermes] [--dry-run]
#
# --device-reachable-as is the address a device reaches THIS bridge on: it becomes the server
# certificate's SAN and the endpoint a device is provisioned with, so it must be the address the
# device can actually route to (LAN IP or a name it resolves), never 127.0.0.1 or 0.0.0.0. It is
# the DIAL address, not the bind address: the registry listener binds 0.0.0.0 by default (override
# with --registry-bind) so a device can reach it on any interface, and a bridge on a host with
# several addresses is reachable without re-provisioning.
# --satellite-host is the old shape (bridge dials the device) and defaults to loopback: it is set
# for deployments that still dial out, and the registry link is the standard path.
#
# Idempotence: a re-run reports what it already did and changes nothing. The bridge.env's managed
# registry/satellite keys move in place to the current flag values (the rest of the file is
# untouched); the Hermes config is backed up before the one edit; and the registry CA is reused,
# never regenerated, because a regenerated CA strands every enrolled device.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE=""                       # the clone of this repository the gateway runs from
REACHABLE_AS=""               # the address devices route to on this host
REGISTRY_PORT="10701"         # the registry listener (plaintext, trusted LAN)
TLS_PORT=""                   # defaults to REGISTRY_PORT+1; the TLS listener devices pin
REGISTRY_BIND="0.0.0.0"       # the address the registry listener BINDS to (the dial address is the cert SAN)
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
    sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --base)                BASE="${2:-}"; shift ;;
        --device-reachable-as) REACHABLE_AS="${2:-}"; shift ;;
        --registry-port)       REGISTRY_PORT="${2:-}"; shift ;;
        --tls-port)            TLS_PORT="${2:-}"; shift ;;
        --registry-bind)       REGISTRY_BIND="${2:-}"; shift ;;
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
# A host named without a port (or a malformed value) leaves the port half empty; the bridge does
# int(VOICE_SATELLITE_PORT) at startup, so refuse it here with a usable message instead of a crash.
case "$SATELLITE_HOST_PORT" in ''|*[!0-9]*) die "satellite host is not host:port: $SATELLITE_HOST" ;; esac
if [ "$SATELLITE_HOST_PORT" -lt 1 ] || [ "$SATELLITE_HOST_PORT" -gt 65535 ]; then
    die "satellite host port out of range 1-65535: $SATELLITE_HOST_PORT"
fi
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
# The keys the installer owns, as KEY=VALUE. VOICE_REGISTRY_HOST is the listener's BIND address
# (REGISTRY_BIND, default 0.0.0.0 -- the address devices route to is the certificate's SAN, not
# this); it is intentionally NOT $REACHABLE_AS, because binding to one interface would break
# devices that reach the host on another. The credentials file path is NOT a key: the bridge
# derives it from its own repo root (state/device-credentials.json), so writing it here would be
# inert.
REGISTRY_KEYS=(
    "VOICE_REGISTRY_ENABLED=1"
    "VOICE_REGISTRY_HOST=$REGISTRY_BIND"
    "VOICE_REGISTRY_PORT=$REGISTRY_PORT"
    "VOICE_REGISTRY_TLS_CERT=$STATE_DIR/registry-tls/server.crt"
    "VOICE_REGISTRY_TLS_KEY=$STATE_DIR/registry-tls/server.key"
    "VOICE_REGISTRY_TLS_CLIENT_CA=$STATE_DIR/registry-tls/client-ca.crt"
    "VOICE_REGISTRY_TLS_PORT=$TLS_PORT"
    "VOICE_SATELLITE_HOST=$SATELLITE_HOST_ADDR"
    "VOICE_SATELLITE_PORT=$SATELLITE_HOST_PORT"
)
# In a dry-run the edits run against a throwaway copy (the only filesystem side effect a dry-run
# performs), so the printed file below is exactly what a real run would leave. In a real run it is
# the live file itself.
if [ "$DRY_RUN" = 1 ]; then
    ENV_EDIT="$(mktemp "${TMPDIR:-/tmp}/bridge-env.XXXXXX")"
    if [ -f "$ENV" ]; then
        cp "$ENV" "$ENV_EDIT"
    else
        cp "$BASE/bridge.env.example" "$ENV_EDIT"
    fi
else
    ENV_EDIT="$ENV"
    [ -f "$ENV_EDIT" ] || cp "$BASE/bridge.env.example" "$ENV_EDIT"
fi
# Set each managed key in place: an existing live KEY=... line is replaced (the rest of the file
# is untouched, so a tuned file is never clobbered), and a key with no live line is appended. The
# template's CHANGE-ME satellite value is thereby filled and its commented registry block
# activated, so the bridge listens the moment the unit starts and no value is left a placeholder.
for kv in "${REGISTRY_KEYS[@]}"; do
    key="${kv%%=*}"; value="${kv#*=}"
    if grep -qE "^[[:space:]]*${key}=" "$ENV_EDIT"; then
        sed -i -E "s|^[[:space:]]*${key}=.*|${key}=${value}|" "$ENV_EDIT"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_EDIT"
    fi
done
if [ "$DRY_RUN" = 1 ]; then
    echo "DRY-RUN: would update $ENV in place (the managed registry/satellite keys move to the current flag values; the rest of the file is untouched). Result:"
    sed 's/^/  /' "$ENV_EDIT"
    rm -f "$ENV_EDIT"
else
    echo "   bridge.env written: $ENV"
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
UNIT_TEMPLATE="$REPO_ROOT/hermes/systemd/hermes-voice-bridge.service.in"
# The unit is rendered from its .in template so its WorkingDirectory / EnvironmentFile / ExecStart
# all point at $BASE -- wherever the gateway actually runs from -- rather than a hardcoded
# %h/hermes-voice-bridge. (The committed hermes-voice-bridge.service is the same unit pinned to the
# documented clone location, for a manual install per the README.)
render_unit() {
    sed "s|__BASE__|$BASE|g" "$UNIT_TEMPLATE"
}
if [ "$DRY_RUN" = 1 ]; then
    echo "DRY-RUN: render $UNIT_TEMPLATE -> $UNIT_DIR/hermes-voice-bridge.service (WorkingDirectory/ExecStart = $BASE)"
    echo "DRY-RUN: systemctl --user daemon-reload && systemctl --user enable --now hermes-voice-bridge.service"
    echo "DRY-RUN: loginctl enable-linger \$USER (so the unit survives logout)"
else
    # The unit reads bridge.env as its EnvironmentFile, so the registry values written in step 2
    # are already visible to the service: no drop-in is needed (and a drop-in with the same values
    # would be a second place to edit, which is how configuration drifts).
    mkdir -p "$UNIT_DIR"
    render_unit > "$UNIT_DIR/hermes-voice-bridge.service"
    chmod 644 "$UNIT_DIR/hermes-voice-bridge.service"
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
    PLUGIN_SRC="$REPO_ROOT/hermes/plugins/voice_satellite"
    PLUGIN_DST="$HOME/.hermes/plugins/voice_satellite"
    if [ "$DRY_RUN" = 1 ]; then
        echo "DRY-RUN: install -m 644 -r $PLUGIN_SRC/. -> $PLUGIN_DST  (the plugin is a local tree, not a catalog entry)"
        echo "DRY-RUN: $HERMES_BIN plugins enable voice_satellite"
        echo "DRY-RUN: apply the voice-surface keys to $HOME/.hermes/config.yaml (backup first)"
    else
        # The plugin is a local tree in this repo, not a catalog / Git / owner:repo entry, so
        # `hermes plugins install` cannot take it (it would try to git-clone the path). Copy it in
        # the way the README documents, then enable it by name.
        run mkdir -p "$PLUGIN_DST"
        run cp -r "$PLUGIN_SRC/." "$PLUGIN_DST/"
        # Drop any stale bytecode shipped in the tree so the destination is clean.
        find "$PLUGIN_DST" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
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
