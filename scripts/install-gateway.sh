#!/usr/bin/env bash
# Install the voice GATEWAY on a host that already runs Hermes: the gateway virtualenv, the
# satellite-gateway.env, the registry TLS material, the systemd user unit, and the Hermes-side plugin +
# config keys.
#
# This is the host-side counterpart of scripts/install-esp32.sh / install-pi.sh. A gateway host
# becomes a gateway in five steps, each visible and idempotent:
#
#   1. venv     -- the Gateway's own virtualenv (faster-whisper, piper, wyoming, ...).
#   2. env      -- satellite-gateway.env from the committed template, with the registry link values filled
#                  in so the Gateway listens the moment the unit starts.
#   3. tls      -- the registry's CA + server certificate and the credential file, minted by
#                  scripts/issue-registry-tls.py (idempotent; an existing CA is never regenerated).
#                  This step also generates the five-word satellite enrolment token (printed ONCE).
#   4. unit     -- the systemd USER unit + the registry drop-in, enabled and started.
#   5. hermes   -- the voice_satellite plugin installed + enabled and the voice-surface keys
#                  applied to the Hermes config, one pass, with a backup.
#
# Usage:
#   scripts/install-gateway.sh --base ~/hermes-voice-satellite --device-reachable-as 192.0.2.10 \
#       [--registry-port 10701] [--tls-port 10702] [--registry-bind 0.0.0.0] \
#       [--satellite-host 192.0.2.20:10700] [--skip-hermes] [--dry-run]
#   scripts/install-gateway.sh            # GUIDED setup: detect, prompt, confirm, then run
#   scripts/install-gateway.sh --guided --dry-run   # the guided flow, previewing only
#
# --device-reachable-as is the address a device reaches THIS Gateway on: it becomes the server
# certificate's SAN and the endpoint a device is provisioned with, so it must be the address the
# device can actually route to (LAN IP or a name it resolves), never 127.0.0.1 or 0.0.0.0. It is
# the DIAL address, not the bind address: the registry listener binds 0.0.0.0 by default (override
# with --registry-bind) so a device can reach it on any interface, and a Gateway on a host with
# several addresses is reachable without re-provisioning.
# --satellite-host is the old shape (Gateway dials the device) and defaults to loopback: it is set
# for deployments that still dial out, and the registry link is the standard path.
#
# GUIDED mode: run with NO arguments (or --guided) and the script detects what it can (host LAN
# IP, an existing checkout or registry state dir, whether `hermes` is on PATH), prompts for the
# rest with defaults shown in [brackets] (bare Enter accepts one), validates each answer with the
# same rules the flags use, prints a summary of exactly what it will do, and asks for an explicit
# yes/no before writing anything. A non-interactive stdin refuses the guided flow -- pass the
# flags for automation. --guided --dry-run previews the guided answers without writing.
#
# Idempotence: a re-run reports what it already did and changes nothing. The satellite-gateway.env's managed
# registry/satellite keys move in place to the current flag values (the rest of the file is
# untouched); the Hermes config is backed up before the one edit; and the registry CA is reused,
# never regenerated, because a regenerated CA strands every enrolled device.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
GUIDED=0
GUIDED_EXPLICIT=0
if [ $# -eq 0 ]; then GUIDED=1; fi
BASE=""                       # the clone of this repository the gateway runs from
REACHABLE_AS=""               # the address devices route to on this host
REGISTRY_PORT="10701"         # the registry listener (plaintext, trusted LAN)
TLS_PORT=""                   # defaults to REGISTRY_PORT+1; the TLS listener devices pin
REGISTRY_BIND="0.0.0.0"       # the address the registry listener BINDS to (the dial address is the cert SAN)
SATELLITE_HOST="127.0.0.1:10700"   # the old shape: where the Gateway dials the satellite
SKIP_HERMES=0
DRY_RUN=0
HERMES_BIN="hermes"
# Flags with no guided prompt of their own are collected verbatim here and forwarded to the
# re-executed run (guided_end): guided mode must run exactly what the user asked for, so
# nothing typed on the command line may be silently dropped.
PASSTHROUGH=()

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
    sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# A value-taking flag must be followed by its value. Without the guard, a trailing `--base`
# (nothing after it) exits 1 with NO message at all: the arm's own shift empties the list and
# the loop's shift below dies under `set -e`. Say what is missing instead.
while [ $# -gt 0 ]; do
    case "$1" in
        --base)                [ $# -ge 2 ] || die "$1 needs a value"; BASE="${2:-}"; shift ;;
        --device-reachable-as) [ $# -ge 2 ] || die "$1 needs a value"; REACHABLE_AS="${2:-}"; shift ;;
        --registry-port)       [ $# -ge 2 ] || die "$1 needs a value"; REGISTRY_PORT="${2:-}"; shift ;;
        --tls-port)            [ $# -ge 2 ] || die "$1 needs a value"; TLS_PORT="${2:-}"; shift ;;
        --registry-bind)       [ $# -ge 2 ] || die "$1 needs a value"; REGISTRY_BIND="${2:-}"; shift ;;
        --satellite-host)      [ $# -ge 2 ] || die "$1 needs a value"; SATELLITE_HOST="${2:-}"; shift ;;
        --hermes-bin)          [ $# -ge 2 ] || die "$1 needs a value"; HERMES_BIN="${2:-}"; PASSTHROUGH+=(--hermes-bin "${2:-}"); shift ;;
        --skip-hermes)         SKIP_HERMES=1 ;;
        --guided)              GUIDED=1; GUIDED_EXPLICIT=1 ;;
        --dry-run)             DRY_RUN=1 ;;
        -h|--help)             usage 0 ;;
        *)                     die "unknown argument: $1 (see --help)" ;;
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
        guided_require_interactive install-gateway.sh
    fi

    echo "Setting up the voice gateway on this machine."
    echo
    echo "This is the part that listens to your satellites, talks to your assistant, and"
    echo "speaks the answers back. I'll set it up and connect it to Hermes."
    echo
    echo "Nothing is changed until you say yes at the end."
    echo
    echo "What I found:"
    LAN_IP="$(guided_detect_lan_ip)"
    if [ -n "$LAN_IP" ]; then
        echo "  host LAN IP:      $LAN_IP (default for --device-reachable-as)"
    else
        echo "  host LAN IP:      not detected (type the address a device routes to)"
    fi
    if [ -d "$REPO_ROOT/satellite_gateway" ] && [ -f "$REPO_ROOT/satellite-gateway.env.example" ] && [ -f "$REPO_ROOT/requirements.txt" ]; then
        echo "  checkout:         $REPO_ROOT (looks like this repository)"
    else
        echo "  checkout:         $REPO_ROOT (incomplete; the gateway must run from a full clone)"
    fi
    if [ -d "$REPO_ROOT/state" ]; then
        echo "  registry state:   $REPO_ROOT/state exists (the CA is reused, never regenerated)"
    else
        echo "  registry state:   not created yet (would become \$BASE/state)"
    fi
    if command -v "$HERMES_BIN" >/dev/null 2>&1; then
        echo "  hermes on PATH:   yes ($(command -v "$HERMES_BIN"))"
    else
        echo "  hermes on PATH:   no (the Hermes-side step needs it; see --hermes-bin / --skip-hermes)"
    fi
    echo

    guided_prompt BASE "Gateway base directory (the clone this gateway runs from)" "${BASE:-$REPO_ROOT}"
    guided_prompt REACHABLE_AS "Address a device reaches THIS Gateway on (LAN IP or resolvable name)" "${REACHABLE_AS:-$LAN_IP}" guided_valid_reachable_as
    guided_prompt REGISTRY_PORT "Registry plaintext listener port" "${REGISTRY_PORT:-10701}" guided_valid_number
    guided_prompt TLS_PORT "Registry TLS listener port (the one devices pin)" "${TLS_PORT:-$((REGISTRY_PORT + 1))}" guided_valid_number
    guided_prompt REGISTRY_BIND "Address the registry listener BINDS to" "${REGISTRY_BIND:-0.0.0.0}"
    guided_prompt SATELLITE_HOST "Satellite dial target, host:port (old shape, for compatibility)" "${SATELLITE_HOST:-127.0.0.1:10700}" guided_valid_hostport
    if [ "$SKIP_HERMES" = 1 ]; then HERMES_YN='n'; else HERMES_YN='y'; fi
    guided_prompt_yn HERMES_YN "Configure the Hermes side (plugin + config keys)?" "$HERMES_YN"

    echo
    echo "=== Summary: install-gateway.sh will run exactly these steps ==="
    echo "  base:           $BASE"
    echo "  reachable as:   $REACHABLE_AS (server certificate SAN + the endpoint devices get)"
    echo "  registry:       plaintext :$REGISTRY_PORT, TLS :$TLS_PORT, bind $REGISTRY_BIND"
    echo "  satellite dial: $SATELLITE_HOST (old shape, for compatibility)"
    if [ "$HERMES_YN" = y ]; then
        echo "  hermes side:    plugin install + voice-surface config keys (config backed up first)"
    else
        echo "  hermes side:    SKIPPED"
    fi
    echo "  1. gateway virtualenv   2. satellite-gateway.env   3. registry TLS   3b. enrolment token   4. systemd user unit   5. Hermes side"
    GUIDED_ARGS=(--base "$BASE" --device-reachable-as "$REACHABLE_AS" --registry-port "$REGISTRY_PORT"
                 --tls-port "$TLS_PORT" --registry-bind "$REGISTRY_BIND" --satellite-host "$SATELLITE_HOST")
    if [ "$HERMES_YN" != y ]; then GUIDED_ARGS+=(--skip-hermes); fi
    # Forward the flags this flow has no prompt for, exactly as given: nothing the user typed
    # on the command line may be silently dropped.
    if [ "${#PASSTHROUGH[@]}" -gt 0 ]; then GUIDED_ARGS+=("${PASSTHROUGH[@]}"); fi
    if [ "$DRY_RUN" = 1 ]; then GUIDED_ARGS+=(--dry-run); fi
    guided_end install-gateway.sh "${GUIDED_ARGS[@]}"
fi

[ -n "$BASE" ] || die "--base is required (the clone of this repository the gateway runs from)"
[ -n "$REACHABLE_AS" ] || die "--device-reachable-as is required (the address a device reaches this Gateway on)"
case "$REACHABLE_AS" in
    127.0.0.1*|::1|localhost*|0.0.0.0*) die "--device-reachable-as must be an address a device can route to, not $REACHABLE_AS (loopback/any never receives a device's dial)" ;;
esac
[ "$DRY_RUN" = 1 ] || [ -d "$BASE/satellite_gateway" ] || die "no satellite_gateway/ under $BASE -- is --base the repository clone?"
[ -f "$BASE/satellite-gateway.env.example" ] || die "no satellite-gateway.env.example at $BASE"
[ -f "$BASE/requirements.txt" ] || die "no requirements.txt at $BASE"

TLS_PORT="${TLS_PORT:-$((REGISTRY_PORT + 1))}"
SATELLITE_HOST_ADDR="$(printf '%s' "$SATELLITE_HOST" | sed 's#:.*$##')"
SATELLITE_HOST_PORT="$(printf '%s' "$SATELLITE_HOST" | sed 's#.*:##')"
# A host named without a port (or a malformed value) leaves the port half empty; the Gateway does
# int(VOICE_SATELLITE_PORT) at startup, so refuse it here with a usable message instead of a crash.
case "$SATELLITE_HOST_PORT" in ''|*[!0-9]*) die "satellite host is not host:port: $SATELLITE_HOST" ;; esac
if [ "$SATELLITE_HOST_PORT" -lt 1 ] || [ "$SATELLITE_HOST_PORT" -gt 65535 ]; then
    die "satellite host port out of range 1-65535: $SATELLITE_HOST_PORT"
fi
STATE_DIR="$BASE/state"
# The venv python, or a placeholder for a dry-run (which never needs the interpreter to exist).
PY="$BASE/.venv/bin/python"

# ---------------------------------------------------------------- 1. venv
echo "== 1. gateway virtualenv ($BASE/.venv) =="
# The Satellite Gateway runs inside its own virtualenv, so this role needs a Python 3 to build one.
# Checking here turns a mid-install "python3: not found" into one sentence about a prerequisite.
# Flashing a device does NOT need this: a standalone bundle carries its own runtime for that.
if [ "$DRY_RUN" = 0 ] && ! command -v python3 >/dev/null 2>&1; then
    die "installing the Satellite Gateway needs Python 3 on this machine, because the service runs in its own virtualenv. Install python3, or run this step on the machine that will host it."
fi
if [ "$DRY_RUN" = 0 ] && [ -x "$BASE/.venv/bin/python" ] && \
   "$BASE/.venv/bin/python" -c 'import wyoming, faster_whisper' 2>/dev/null; then
    echo "   venv already has wyoming + faster-whisper; leaving it alone"
else
    run python3 -m venv "$BASE/.venv"
    run "$BASE/.venv/bin/python" -m pip install -r "$BASE/requirements.txt"
    run "$BASE/.venv/bin/python" -m pip check
    # The Piper voice the Gateway speaks with (not committed; the unit's WorkingDirectory expects
    # voices/ next to gateway/). The name follows the installation's configuration — the same
    # VOICE_PIPER_VOICE resolution satellite_gateway/config.py uses: an existing satellite-gateway.env wins, otherwise
    # the code default. Changing the voice is docs/switching-voices.md; this line must not grow
    # its own default. satellite_gateway/config.py needs only the stdlib, so either interpreter can read it.
    VOICE_PY=""
    if [ -x "$BASE/.venv/bin/python" ]; then VOICE_PY="$BASE/.venv/bin/python"; else VOICE_PY="python3"; fi
    PIPER_VOICE_NAME="$("$VOICE_PY" -c '
import sys; sys.path.insert(0, sys.argv[1])
from satellite_gateway.config import PIPER_VOICE_NAME
print(PIPER_VOICE_NAME)' "$BASE" 2>/dev/null || true)"
    if [ -z "$PIPER_VOICE_NAME" ]; then
        echo "   could not read the configured voice name from satellite_gateway/config.py; skipping the voice download" >&2
    elif "$VOICE_PY" "$BASE/scripts/fetch-voice.py" --is-external "$PIPER_VOICE_NAME" >/dev/null 2>&1; then
        # Voices outside the Piper catalog (clean100): fetched with pinned sha256 and a
        # 22,050 Hz contract check, because piper.download_voices cannot know them.
        echo "   Gateway voice: $PIPER_VOICE_NAME (curated external voice, hash-pinned)"
        run "$VOICE_PY" "$BASE/scripts/fetch-voice.py" "$PIPER_VOICE_NAME" --download-dir "$BASE/voices"
    else
        echo "   Gateway voice: $PIPER_VOICE_NAME"
        run "$BASE/.venv/bin/python" -m piper.download_voices --download-dir "$BASE/voices" "$PIPER_VOICE_NAME"
    fi
fi

# ---------------------------------------------------------------- 2. satellite-gateway.env
echo "== 2. satellite-gateway.env ($BASE/satellite-gateway.env) =="
ENV="$BASE/satellite-gateway.env"
# The keys the installer owns, as KEY=VALUE. VOICE_REGISTRY_HOST is the listener's BIND address
# (REGISTRY_BIND, default 0.0.0.0 -- the address devices route to is the certificate's SAN, not
# this); it is intentionally NOT $REACHABLE_AS, because binding to one interface would break
# devices that reach the host on another. The credentials file path is NOT a key: the Gateway
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
    ENV_EDIT="$(mktemp "${TMPDIR:-/tmp}/satellite-gateway-env.XXXXXX")"
    if [ -f "$ENV" ]; then
        cp "$ENV" "$ENV_EDIT"
    else
        cp "$BASE/satellite-gateway.env.example" "$ENV_EDIT"
    fi
else
    ENV_EDIT="$ENV"
    [ -f "$ENV_EDIT" ] || cp "$BASE/satellite-gateway.env.example" "$ENV_EDIT"
fi
# Set each managed key in place: an existing live KEY=... line is replaced (the rest of the file
# is untouched, so a tuned file is never clobbered), and a key with no live line is appended. The
# template's CHANGE-ME satellite value is thereby filled and its commented registry block
# activated, so the Gateway listens the moment the unit starts and no value is left a placeholder.
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
    echo "   satellite-gateway.env written: $ENV"
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

# ---------------------------------------------------------------- 3b. enrolment token
# The five-word token a new satellite is set up with: generated once here, printed ONCE, and
# stored 0600 at state/enroll-token.txt (the Gateway regenerates it lazily if this step never
# ran, but then nobody has seen it). Never re-printed on a re-run and never overwritten: a new
# token would strand every satellite already enrolled under the old one.
echo "== 3b. satellite enrolment token ($STATE_DIR/enroll-token.txt) =="
ENROLL_TOKEN_FILE="$STATE_DIR/enroll-token.txt"
if [ "$DRY_RUN" = 1 ]; then
    echo "DRY-RUN: $PY $REPO_ROOT/scripts/generate-enroll-token.py --words $REPO_ROOT/scripts/data/enroll-words.txt --out $ENROLL_TOKEN_FILE"
    if [ -f "$ENROLL_TOKEN_FILE" ]; then
        echo "   an enrolment token already exists at $ENROLL_TOKEN_FILE (never re-printed)"
    fi
elif [ -f "$ENROLL_TOKEN_FILE" ] && [ -s "$ENROLL_TOKEN_FILE" ]; then
    echo "   an enrolment token already exists at $ENROLL_TOKEN_FILE (never re-printed)"
else
    ENROLL_TOKEN="$("$PY" "$REPO_ROOT/scripts/generate-enroll-token.py" \
        --words "$REPO_ROOT/scripts/data/enroll-words.txt" --out "$ENROLL_TOKEN_FILE")"
    echo
    echo "=================================================================="
    echo "  SATELLITE ENROLMENT TOKEN -- shown ONCE, kept only at"
    echo "  $ENROLL_TOKEN_FILE (mode 600):"
    echo
    echo "      $ENROLL_TOKEN"
    echo
    echo "  You will need this when you set up a satellite."
    echo "=================================================================="
fi

# ---------------------------------------------------------------- 4. systemd user unit
echo "== 4. systemd user unit (hermes-voice-satellite-gateway.service) =="
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_TEMPLATE="$REPO_ROOT/hermes/systemd/hermes-voice-satellite-gateway.service.in"
# The unit is rendered from its .in template so its WorkingDirectory / EnvironmentFile / ExecStart
# all point at $BASE -- wherever the gateway actually runs from -- rather than a hardcoded
# %h/hermes-voice-satellite. (The committed hermes-voice-satellite-gateway.service is the same unit pinned to the
# documented clone location, for a manual install per the README.)
render_unit() {
    sed "s|__BASE__|$BASE|g" "$UNIT_TEMPLATE"
}
if [ "$DRY_RUN" = 1 ]; then
    echo "DRY-RUN: render $UNIT_TEMPLATE -> $UNIT_DIR/hermes-voice-satellite-gateway.service (WorkingDirectory/ExecStart = $BASE)"
    echo "DRY-RUN: systemctl --user daemon-reload && systemctl --user enable --now hermes-voice-satellite-gateway.service"
    echo "DRY-RUN: loginctl enable-linger \$USER (so the unit survives logout)"
else
    # The unit reads satellite-gateway.env as its EnvironmentFile, so the registry values written in step 2
    # are already visible to the service: no drop-in is needed (and a drop-in with the same values
    # would be a second place to edit, which is how configuration drifts).
    mkdir -p "$UNIT_DIR"
    render_unit > "$UNIT_DIR/hermes-voice-satellite-gateway.service"
    chmod 644 "$UNIT_DIR/hermes-voice-satellite-gateway.service"
    systemctl --user daemon-reload
    systemctl --user enable --now hermes-voice-satellite-gateway.service
    loginctl enable-linger "$USER" 2>/dev/null || echo "   note: enable lingering manually (loginctl enable-linger $USER)"
    echo "   unit status:"
    systemctl --user --no-pager status hermes-voice-satellite-gateway.service | head -6
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
