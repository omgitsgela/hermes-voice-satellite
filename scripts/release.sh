#!/usr/bin/env bash
# Cut a release: verify the tree, run the gate, build the installer, verify the
# ESP32 flash bundle, tag, and publish both as release assets.
#
# Usage:
#   scripts/release.sh --check                     # what would be released, changes nothing
#   scripts/release.sh --dry-run --flash-bundle ~/xvf3800-flash
#   scripts/release.sh --flash-bundle ~/xvf3800-flash
#
# Flags:
#   --flash-bundle DIR   the reviewed bundle directory (flash.bundle.bin +
#                        flash.manifest.txt). Required to publish, because the
#                        installation guide tells readers the prebuilt image
#                        ships as a release asset.
#   --check              report version, tag state, artifacts and notes; no writes
#   --dry-run            print the release commands; no tag, no upload
#   --skip-gate          do not run scripts/check-all.sh (only for a re-run after
#                        the gate passed at this exact commit)
#   --allow-dirty        publish from a dirty tree (refused by default)
#
# The version comes from packages/installer/pyproject.toml, so bumping the four
# package versions is the only version bookkeeping.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

FLASH_BUNDLE=""
CHECK=0
DRY=0
SKIP_GATE=0
ALLOW_DIRTY=0

say() { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die() { printf 'release: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --flash-bundle) [ $# -ge 2 ] || die "--flash-bundle needs a directory"; FLASH_BUNDLE="${2:-}"; shift ;;
        --check)        CHECK=1 ;;
        --dry-run)      DRY=1 ;;
        --skip-gate)    SKIP_GATE=1 ;;
        --allow-dirty)  ALLOW_DIRTY=1 ;;
        -h|--help)      sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)              die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

command -v gh >/dev/null 2>&1 || die "gh is not installed"
command -v git >/dev/null 2>&1 || die "git is not installed"

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' packages/installer/pyproject.toml | head -1)"
[ -n "$VERSION" ] || die "cannot read the version from packages/installer/pyproject.toml"
TAG="v$VERSION"
HEAD="$(git rev-parse --short HEAD)"
DIRTY="$(git status --porcelain)"

# The four packages move together; a mismatch ships an installer whose parts
# disagree about what they are.
for f in packages/gateway packages/device-esp32 packages/device-pi; do
    v="$(sed -n 's/^version = "\(.*\)"/\1/p' "$f/pyproject.toml" | head -1)"
    [ "$v" = "$VERSION" ] || die "$f is $v but the installer is $VERSION"
done

PUBLISHED_BY_CHECK=0
gh release view "$TAG" >/dev/null 2>&1 && PUBLISHED_BY_CHECK=1

step "1. State"
say "version:  $VERSION"
say "tag:      $TAG ($([ "$PUBLISHED_BY_CHECK" = 1 ] && echo already published || echo not published))"
say "commit:   $HEAD on $(git rev-parse --abbrev-ref HEAD)"
say "tree:     $([ -n "$DIRTY" ] && echo DIRTY || echo clean)"

if [ "$CHECK" = 1 ]; then
    say "notes:    CHANGELOG.md has a [$VERSION] section: $([ -n "$(sed -n "/^## \[$VERSION\]/p" CHANGELOG.md)" ] && echo yes || echo NO)"
    if [ -n "$FLASH_BUNDLE" ] && [ -f "$FLASH_BUNDLE/flash.bundle.bin" ]; then
        say "bundle:   $FLASH_BUNDLE/flash.bundle.bin ($(wc -c < "$FLASH_BUNDLE/flash.bundle.bin") bytes)"
    else
        say "bundle:   not supplied (--flash-bundle), so a publish would stop here"
    fi
    say "gate:     $([ "$SKIP_GATE" = 1 ] && echo skipped || echo would run)"
    exit 0
fi

step "2. Preconditions"
[ -n "$DIRTY" ] && [ "$ALLOW_DIRTY" != 1 ] && die "working tree is dirty; commit first (or --allow-dirty)"
[ "$PUBLISHED_BY_CHECK" = 1 ] && die "$TAG is already published"
[ -n "$(sed -n "/^## \[$VERSION\]/p" CHANGELOG.md)" ] || die "CHANGELOG.md has no [$VERSION] section"
[ -n "$FLASH_BUNDLE" ] || die "--flash-bundle DIR is required to publish (the prebuilt image is a release asset)"
[ -f "$FLASH_BUNDLE/flash.bundle.bin" ] || die "no flash.bundle.bin in $FLASH_BUNDLE"
[ -f "$FLASH_BUNDLE/flash.manifest.txt" ] || die "no flash.manifest.txt in $FLASH_BUNDLE"
say "preconditions met"

step "3. Gate"
if [ "$SKIP_GATE" = 1 ]; then
    say "skipped (--skip-gate); the gate must have passed at $HEAD"
else
    bash scripts/check-all.sh || die "the gate did not pass; nothing was tagged"
fi

step "4. Installer bundle"
bash scripts/build-installer.sh || die "installer build failed"
env -i PATH=/usr/bin:/bin HOME=/tmp dist/hermes-voice-satellite/hermes-voice-satellite --check-assets \
    || die "the built installer is missing runtime assets"
TARBALL="dist/hermes-voice-satellite-$VERSION-linux-x86_64.tar.gz"
tar -C dist -czf "$TARBALL" hermes-voice-satellite || die "could not package the installer"
say "installer: $TARBALL ($(wc -c < "$TARBALL") bytes)"

step "5. Flash bundle"
.venv/bin/python scripts/bundle-esp32-flash.py --verify "$FLASH_BUNDLE/flash.bundle.bin" \
    --check "$FLASH_BUNDLE/flash.manifest.txt" || die "the flash bundle does not match its manifest"
BUNDLE_SHA="$(sha256sum "$FLASH_BUNDLE/flash.bundle.bin" | cut -d' ' -f1)"
say "bundle sha256: $BUNDLE_SHA"

step "6. Tag and publish"
if [ "$DRY" = 1 ]; then
    say "[dry-run] git tag -a $TAG -m \"Hermes Voice Satellite $VERSION\""
    say "[dry-run] gh release create $TAG $TARBALL $FLASH_BUNDLE/flash.bundle.bin $FLASH_BUNDLE/flash.manifest.txt --title ... --notes-file <changelog section>"
    exit 0
fi
git tag -a "$TAG" -m "Hermes Voice Satellite $VERSION" || die "could not create $TAG"
NOTES="$(mktemp)"
trap 'rm -f "$NOTES"' EXIT
awk -v ver="$VERSION" '
    $0 ~ "^## \\[" ver "\\]" { inside = 1; next }
    inside && /^## \[/ { exit }
    inside { print }
' CHANGELOG.md > "$NOTES"
[ -s "$NOTES" ] || die "could not extract the changelog section"
git push origin "$TAG" || die "could not push $TAG"
gh release create "$TAG" "$TARBALL" "$FLASH_BUNDLE/flash.bundle.bin" "$FLASH_BUNDLE/flash.manifest.txt" \
    --title "Hermes Voice Satellite $VERSION" --notes-file "$NOTES" \
    || die "the release was tagged but gh release create failed; the tag is pushed, retry the upload"
say "published $TAG"
say "read it back: gh release view $TAG --json assets"
