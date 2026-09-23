#!/usr/bin/env bash
# Read-only report: which patches in this directory are applied to the Hermes checkout right now.
# Exit 0 when everything expected is applied, 1 when something is missing or needs review.
set -u
REPO="${HERMES_REPO:-$HOME/.hermes/hermes-agent}"
HERE="$(cd "$(dirname "$0")" && pwd)"

cd "$REPO" 2>/dev/null || { echo "cannot enter $REPO"; exit 2; }

status=0
for p in "$HERE"/*.patch; do
    [ -e "$p" ] || continue
    name="$(basename "$p")"
    if git apply --check --reverse "$p" >/dev/null 2>&1; then
        echo "  applied   $name"
    elif git apply --check "$p" >/dev/null 2>&1; then
        echo "  MISSING   $name"
        status=1
    else
        echo "  REVIEW    $name  (neither applies nor reverses cleanly: upstream changed, patch by hand)"
        status=1
    fi
done
exit "$status"
