#!/usr/bin/env bash
# Regression guard for issue #45: the repository must stay general.
#
# The documentation was generalised in 2026-09-24, and this script is what keeps the *code* that
# way. It fails if any tracked file (the vendored upstream copies under pi/vendor/ and reference/
# are excluded, because those are kept byte-for-byte as upstream shipped them) still carries an
# installation-specific value that belongs in a config file instead of in source:
#
#   * a private LAN address from this installation,
#   * an absolute /home/<user>/ path,
#   * the real satellite's name,
#   * a personal name (legal copyright / third-party attribution in LICENSE and THIRD-PARTY.md is
#     allowed and is not flagged).
#
# Those values now live in bridge.env (the host side, template in bridge.env.example) and
# /etc/voice-satellite/satellite.env (the device side, template in pi/systemd/satellite.env), with
# the committed templates carrying <...> placeholders. Run it the same way as
# hermes/patches/check-patches.sh: it is read-only and exits non-zero when something has leaked
# back in.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 2

FILES="$(git ls-files | grep -v -e '^pi/vendor/' -e '^reference/')"
if [ -z "$FILES" ]; then
  echo "check-no-personal-data: no tracked files found; not running in a repo?" >&2
  exit 2
fi

fail=0

# 1. A private LAN address from this installation.
if printf '%s\n' "$FILES" | xargs grep -nE '192\.168\.0\.' 2>/dev/null | grep -q .; then
  echo "FAIL: a 192.168.0.x address is in a tracked file (it belongs in a config file):"
  printf '%s\n' "$FILES" | xargs grep -nE '192\.168\.0\.' 2>/dev/null
  fail=1
fi

# 2. An absolute home directory path. (The <...> template placeholders do not match: a '<' is not
# a letter, so bridge.env.example and pi/systemd/satellite.env stay clean.)
if printf '%s\n' "$FILES" | xargs grep -nE '/home/[a-z][a-z0-9_-]*/' 2>/dev/null | grep -q .; then
  echo "FAIL: an absolute /home/<user>/ path is in a tracked file:"
  printf '%s\n' "$FILES" | xargs grep -nE '/home/[a-z][a-z0-9_-]*/' 2>/dev/null
  fail=1
fi

# 3. The real satellite's name. Example values (e.g. "satellite") and variables are fine; the real
# device name is not.
if printf '%s\n' "$FILES" | xargs grep -nw 'wyoming1' 2>/dev/null | grep -q .; then
  echo "FAIL: the real device name 'wyoming1' is in a tracked file (use an example or a variable):"
  printf '%s\n' "$FILES" | xargs grep -nw 'wyoming1' 2>/dev/null
  fail=1
fi

# 4. A personal name, except legal attribution in the two licence files.
if printf '%s\n' "$FILES" | xargs grep -nE '\bAngela\b|\bMichael\b' 2>/dev/null \
    | grep -vE '^(LICENSE|THIRD-PARTY\.md):' | grep -q .; then
  echo "FAIL: a personal name is in a tracked file (legal attribution in LICENSE/THIRD-PARTY.md is allowed):"
  printf '%s\n' "$FILES" | xargs grep -nE '\bAngela\b|\bMichael\b' 2>/dev/null | grep -vE '^(LICENSE|THIRD-PARTY\.md):'
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "check-no-personal-data: clean (no installation-specific values in tracked files)"
  exit 0
fi
echo "check-no-personal-data: see failures above"
exit 1
