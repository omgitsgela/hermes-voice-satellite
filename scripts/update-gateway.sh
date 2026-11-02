#!/usr/bin/env bash
# Update an existing Satellite Gateway checkout to a newer revision, safely.
#
# This is the upgrade counterpart of install-gateway.sh. It moves CODE and
# DEPENDENCIES and restarts the service; it does not rewrite your configuration.
# satellite-gateway.env, the registry TLS material, the enrolment token and the
# device credentials are yours and are left exactly as they are. If the update
# changes the systemd unit template, this script says so and install-gateway.sh
# is the step that re-renders it (idempotently, without touching your env).
#
# Usage:
#   scripts/update-gateway.sh --base ~/hermes-voice-satellite
#   scripts/update-gateway.sh --base ~/hermes-voice-satellite --ref origin/main
#   scripts/update-gateway.sh --base ~/hermes-voice-satellite --check
#   scripts/update-gateway.sh --base ~/hermes-voice-satellite --dry-run
#
# Flags:
#   --base DIR     the checkout to update (default: the parent of this script)
#   --ref REF      the revision to move to (default: origin/main)
#   --check        report the current revision, how far behind it is, and the
#                  service state; fetches remote refs so the answer is current,
#                  but changes nothing in the working tree
#   --dry-run      print every step and run none of them (no fetch, no writes)
#   --no-restart   update the tree and dependencies, leave the service alone
#   --allow-dirty  proceed with uncommitted changes (they are kept; a pull that
#                  would overwrite them still fails)
#
# Exit status: 0 updated (or already current), 1 failed, 2 refused (bad input,
# dirty tree, not a checkout).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="$ROOT"
REF="origin/main"
CHECK=0
DRY=0
RESTART=1
ALLOW_DIRTY=0
UNIT="hermes-voice-satellite-gateway.service"

say() { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die() { printf 'update-gateway: %s\n' "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --base)       [ $# -ge 2 ] || die "--base needs a directory"; BASE="${2:-}"; shift ;;
        --ref)        [ $# -ge 2 ] || die "--ref needs a revision"; REF="${2:-}"; shift ;;
        --check)      CHECK=1 ;;
        --dry-run)    DRY=1 ;;
        --no-restart) RESTART=0 ;;
        --allow-dirty) ALLOW_DIRTY=1 ;;
        -h|--help)    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

BASE="$(cd "$BASE" 2>/dev/null && pwd)" || die "no such directory: $BASE"
[ -d "$BASE/.git" ] || die "$BASE is not a git checkout (install from a clone, or re-run the installer)"
command -v git >/dev/null 2>&1 || die "git is not installed"

cd "$BASE" || die "cannot enter $BASE"

current="$(git rev-parse --short HEAD 2>/dev/null)" || die "cannot read HEAD in $BASE"
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
dirty="$(git status --porcelain 2>/dev/null || true)"

run() {
    if [ "$DRY" = 1 ]; then
        printf '  [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

service_state() {
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user is-active "$UNIT" 2>/dev/null || echo "inactive"
    else
        echo "no systemctl"
    fi
}

if [ "$CHECK" = 1 ]; then
    printf 'checkout:   %s\n' "$BASE"
    printf 'revision:   %s on %s\n' "$current" "$branch"
    [ -n "$dirty" ] && printf 'working tree: DIRTY (uncommitted changes)\n' || printf 'working tree: clean\n'
    if [ "$DRY" = 0 ]; then
        git fetch --quiet origin 2>/dev/null || true
    fi
    if git rev-parse --verify --quiet "$REF" >/dev/null; then
        counts="$(git rev-list --left-right --count "HEAD...$REF" 2>/dev/null || echo "? ?")"
        ahead="${counts%%[[:space:]]*}"
        behind="${counts##*[[:space:]]}"
        printf 'ref %-10s %s: %s ahead, %s behind\n' "$REF" \
            "$(git rev-parse --short "$REF" 2>/dev/null)" "$ahead" "$behind"
    else
        printf 'ref %-10s not found (fetch failed, or a bad ref)\n' "$REF"
    fi
    printf 'service:    %s (%s)\n' "$UNIT" "$(service_state)"
    exit 0
fi

step "1. Preconditions"
say "checkout: $BASE"
say "revision: $current on $branch"
if [ -n "$dirty" ]; then
    if [ "$ALLOW_DIRTY" != 1 ]; then
        say "uncommitted changes present; commit or stash them, or pass --allow-dirty"
        exit 2
    fi
    say "uncommitted changes present; keeping them (--allow-dirty)"
fi

step "2. Fetch and fast-forward to $REF"
run git fetch --tags origin || die "git fetch failed"
if ! git rev-parse --verify --quiet "$REF" >/dev/null; then
    die "no such revision after fetch: $REF"
fi
target="$(git rev-parse --short "$REF")"
if [ "$(git rev-parse HEAD)" = "$(git rev-parse "$REF")" ]; then
    say "already at $target; nothing to move"
else
    run git merge --ff-only "$REF" || die "cannot fast-forward to $REF (diverged history: merge by hand)"
    say "moved to $target"
fi

step "3. Dependencies"
if [ -x "$BASE/.venv/bin/python" ]; then
    run "$BASE/.venv/bin/python" -m pip install --quiet --upgrade -r "$BASE/requirements.txt" \
        || die "dependency install failed"
    run "$BASE/.venv/bin/python" -m pip check || die "pip check reported a broken environment"
    say "virtualenv updated and consistent"
else
    say "no .venv here; the installer creates one (install-gateway.sh)"
fi

step "4. Service"
if git diff --quiet "$current" "$REF" -- hermes/systemd 2>/dev/null; then
    say "unit template unchanged"
else
    say "unit template changed: re-run install-gateway.sh to re-render it (it keeps your env)"
fi
if [ "$RESTART" != 1 ]; then
    say "restart skipped (--no-restart)"
elif ! command -v systemctl >/dev/null 2>&1; then
    say "no systemctl on this host; restart the service yourself"
else
    run systemctl --user daemon-reload || die "daemon-reload failed"
    run systemctl --user restart "$UNIT" || die "restart failed"
    for _ in $(seq 1 20); do
        if systemctl --user is-active --quiet "$UNIT"; then break; fi
        sleep 0.25
    done
    if systemctl --user is-active --quiet "$UNIT"; then
        say "service active"
    else
        say "service is NOT active after restart; recent log:"
        journalctl --user -u "$UNIT" -n 20 --no-pager 2>/dev/null | tail -20
        exit 1
    fi
fi

step "Done"
say "revision now: $(git rev-parse --short HEAD)"
say "verify what the running process loaded before calling it deployed: a restart is not proof"
