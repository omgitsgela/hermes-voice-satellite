#!/bin/sh
# Guard issue #45: deployment values belong in local env files, not tracked source.
# Vendored/reference copies stay byte-for-byte upstream; legal name attribution is allowed.
#
# Two scopes, because a secret can hide in a file the tree has not yet claimed:
#   (default) tracked files only, via git grep -- the long-standing behaviour;
#   --untracked  untracked, non-ignored files (git ls-files --others --exclude-standard):
#                this is the mode that catches a value in a file that has not been staged,
#                which is exactly the file the tracked scan cannot see;
#   --all        both.
# set -u
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT" || exit 2
git rev-parse --show-toplevel >/dev/null || exit 2
fail=0

DO_TRACKED=1
DO_UNTRACKED=0
for arg in "$@"; do
    case "$arg" in
        --untracked) DO_TRACKED=0; DO_UNTRACKED=1 ;;
        --all)       DO_UNTRACKED=1 ;;
        --tracked)   DO_UNTRACKED=0 ;;
        *) echo "check-no-personal-data: unknown option $arg (use --untracked or --all)" >&2; exit 2 ;;
    esac
done

# Vendor and reference trees are byte-for-byte upstream and are never scanned, in either scope.
EXCLUDE='^(pi/vendor/|reference/)'

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

check_untracked() {
    label=$1
    pattern=$2
    # $3 is an optional file-name regex to skip (e.g. the legal-attribution files for the name
    # check). A file that is not a regular file (a symlink to a directory) is not a scan target.
    skip=${3:-^$}
    [ "$DO_UNTRACKED" = 1 ] || return 0
    files=$(git ls-files --others --exclude-standard | grep -v -E "$EXCLUDE" | grep -v -E "$skip" || true)
    [ -n "$files" ] || return 0
    hits=$(printf '%s\n' "$files" | while IFS= read -r f; do
        [ -n "$f" ] || continue
        [ -f "$f" ] || continue
        if grep -I -i -E "$pattern" -- "$f" >/dev/null 2>&1; then
            printf '%s\n' "$f"
        fi
    done)
    if [ -n "$hits" ]; then
        printf 'FAIL: %s is in an untracked, non-ignored file (remove it or gitignore it before committing):\n' "$label"
        printf '%s\n' "$hits" | sed 's/^/    /'
        fail=1
    fi
}

if [ "$DO_TRACKED" = 1 ]; then
    # Character classes avoid matching the guard's own patterns, without excluding this file.
    check 'private LAN address' '192\.168\.0\.'
    check 'absolute home directory' '/home/[a-z_][a-z0-9_.-]*(/|[^a-z0-9_.-]|$)'
    check 'real device name' '\bwyoming[1]\b'
    check 'personal name' '\bAn[g]ela\b|\bMi[c]hael\b' ':!LICENSE' ':!THIRD-PARTY.md'
fi

if [ "$DO_UNTRACKED" = 1 ]; then
    check_untracked 'private LAN address' '192\.168\.0\.'
    check_untracked 'absolute home directory' '/home/[a-z_][a-z0-9_.-]*(/|[^a-z0-9_.-]|$)'
    check_untracked 'real device name' '\bwyoming[1]\b'
    check_untracked 'personal name' '\bAn[g]ela\b|\bMi[c]hael\b' '^(LICENSE|THIRD-PARTY\.md)$'
fi

if [ "$fail" -eq 0 ]; then
    echo 'check-no-personal-data: clean (no installation-specific values in the scanned scope)'
fi
exit "$fail"
