#!/bin/sh
# Guard issue #45: deployment values belong in local env files, not tracked source.
# Vendored/reference copies stay byte-for-byte upstream; legal name attribution is allowed.
set -u
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT" || exit 2
git rev-parse --show-toplevel >/dev/null || exit 2
fail=0

check() {
    label=$1
    pattern=$2
    shift 2
    # git grep handles tracked filenames directly (spaces/newlines/quotes included).
    # Do not suppress errors or convert an unreadable scan into a clean result.
    git grep -n -I -i -E "$pattern" -- . ':!pi/vendor/**' ':!reference/**' "$@"
    status=$?
    case "$status" in
        0) printf 'FAIL: %s is in a tracked file (use local configuration)\n' "$label"; fail=1 ;;
        1) : ;;
        *) echo 'check-no-personal-data: scan failed' >&2; exit 2 ;;
    esac
}

# Character classes avoid matching the guard's own patterns, without excluding this file.
check 'private LAN address' '192\.168\.0\.'
check 'absolute home directory' '/home/[a-z_][a-z0-9_.-]*(/|[^a-z0-9_.-]|$)'
check 'real device name' '\bwyoming[1]\b'
check 'personal name' '\bAn[g]ela\b|\bMi[c]hael\b' ':!LICENSE' ':!THIRD-PARTY.md'

if [ "$fail" -eq 0 ]; then
    echo 'check-no-personal-data: clean (no installation-specific values in tracked files)'
fi
exit "$fail"
