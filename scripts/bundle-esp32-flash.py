#!/usr/bin/env python3
"""Bundle a firmware build into a reviewable flash artifact, and check it against review.

Flashing is the one act in this project with teeth, and it waits for a gate. What the
gate needs is an artifact that can be reviewed without trusting a remembered build: the
exact set of files that will be written to the chip, at their exact offsets, with their
sizes and hashes, produced from the build's OWN flash map (flasher_args.json) rather than
a list that can drift from it.

The manifest is self-contained (offset, file name, size, sha256 per line, in flash-map
order). --check re-reads the files from the build and re-hashes them against a reviewed
manifest: if a byte of the app changed after review, the check fails and names the file.
The gate is then a hash comparison, not a memory.

--bundle writes the components into one image, each placed at its flash offset with
zero padding between, so a flasher can write the bundle straight to 0x0. The bundle's own
sha256 is written into the manifest as a 'bundle_sha256=' header line and reported, so a
published image can be pinned -- and a user who downloaded a prebuilt bundle can verify it
WITHOUT the source build tree, which is the whole point of shipping a prebuilt image.

Usage:
    bundle-esp32-flash.py --build <build-dir>            # print the manifest
    bundle-esp32-flash.py --build <build-dir> --out manifest.txt
    bundle-esp32-flash.py --build <build-dir> --bundle bundle.bin --out manifest.txt
    bundle-esp32-flash.py --build <build-dir> --check manifest.txt   # re-hash the source build
    bundle-esp32-flash.py --verify bundle.bin --check manifest.txt   # a prebuilt image, no build
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path


def load_flash_map(build):
    """The build's own flash map: (offset, name) in flash order, or a refusal. The map is a
    dict of offset to path relative to the build directory, exactly as idf.py writes it.
    Refusal rather than a guessed map: a flasher that writes files at the wrong offsets is
    not a recoverable mistake, and the build's map is the only authority for where they go."""
    path = build / 'flasher_args.json'
    if not path.exists():
        raise SystemExit(f'no flash map: {path} is missing; build the firmware first '
                         '(IDF_TARGET=esp32s3 idf.py build writes flasher_args.json)')
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f'flash map unreadable: {path}: {error}')
    entries = data.get('flash_files')
    if not isinstance(entries, dict) or not entries:
        raise SystemExit(f'flash map has no entries: {path}')
    mapped = []
    for offset_text, name in entries.items():
        try:
            offset = int(offset_text, 16)
        except (TypeError, ValueError):
            raise SystemExit(f'flash map entry has no usable offset: {offset_text!r}')
        target = build / name
        if not target.exists():
            raise SystemExit(f'flash map names a missing file: {name} '
                             f'(a file that is not built cannot be bundled or checked)')
        mapped.append((offset, name))
    mapped.sort(key=lambda pair: pair[0])
    return mapped


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_line(offset, build, name):
    target = build / name
    return (f'offset=0x{offset:x} name={name} size={target.stat().st_size} '
            f'sha256={sha256_of(target)}')


def emit_manifest(build, out_path=None, bundle_sha256=None):
    mapped = load_flash_map(build)
    lines = [manifest_line(offset, build, name) for offset, name in mapped]
    # The bundle's own hash rides along as an optional header line. It is what lets a
    # prebuilt image be verified with no build tree (--verify); absent it, the manifest
    # still names every component, and --check still re-hashes a source build.
    if bundle_sha256:
        lines = [f'bundle_sha256={bundle_sha256}'] + lines
    text = '\n'.join(lines) + '\n'
    if out_path:
        Path(out_path).write_text(text)
        print(f'manifest={out_path} entries={len(mapped)}')
    for line in lines:
        print(line)
    return lines


def parse_manifest(path):
    """Return (component entries, bundle_sha256 or None). The 'bundle_sha256=' header, when
    present, is pulled out rather than parsed as a component line."""
    entries = []
    bundle_sha256 = None
    for raw in Path(path).read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith('bundle_sha256='):
            bundle_sha256 = raw.split('=', 1)[1].strip()
            continue
        fields = dict(field.split('=', 1) for field in raw.split())
        for required in ('offset', 'name', 'size', 'sha256'):
            if required not in fields:
                raise SystemExit(f'manifest line is missing {required}: {raw}')
        entries.append((int(fields['offset'], 16), fields['name'],
                        int(fields['size']), fields['sha256']))
    if not entries:
        raise SystemExit(f'manifest is empty: {path}')
    return entries, bundle_sha256


def write_bundle(build, mapped, bundle_path):
    highest = max(offset + (build / name).stat().st_size for offset, name in mapped)
    image = bytearray(highest)
    for offset, name in mapped:
        data = (build / name).read_bytes()
        image[offset:offset + len(data)] = data
    Path(bundle_path).write_bytes(bytes(image))
    return len(image)


def check_manifest(build, manifest_path):
    """Re-hash every file a reviewed manifest names and compare. Any mismatch -- size or
    hash -- is a refusal that names the file, because the reviewed bytes and the bytes on
    disk have stopped agreeing."""
    entries, _bundle_sha256 = parse_manifest(manifest_path)
    mismatches = 0
    for offset, name, size, expected in entries:
        target = build / name
        if not target.exists():
            print(f'MISMATCH {name}: missing from the build')
            mismatches += 1
            continue
        actual = sha256_of(target)
        actual_size = target.stat().st_size
        if actual != expected or actual_size != size:
            print(f'MISMATCH {name}: reviewed sha256={expected} size={size}; '
                  f'now sha256={actual} size={actual_size}')
            mismatches += 1
        else:
            print(f'OK {name} offset=0x{offset:x}')
    if mismatches:
        raise SystemExit(f'{mismatches} file(s) disagree with the reviewed manifest')
    print('manifest check: all files match')


def verify_bundle(bundle_path, manifest_path):
    """Verify a standalone (prebuilt) bundle against the manifest's own bundle_sha256 header.
    This is the gate for a user who flashed a published image and never built the source: the
    one file that goes to 0x0 is hashed and compared, with no build tree required."""
    _entries, expected = parse_manifest(manifest_path)
    if not expected:
        raise SystemExit(f'manifest has no bundle_sha256 line: {manifest_path} '
                         '(regenerate it with --bundle, or run --check with the source build)')
    actual = sha256_of(Path(bundle_path))
    if actual != expected:
        raise SystemExit(f'bundle disagrees with the reviewed manifest: '
                         f'reviewed sha256={expected} now sha256={actual}')
    print(f'OK bundle {bundle_path} sha256={actual}')
    print('bundle check: the prebuilt image matches the reviewed manifest')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--build', help='the build directory (flasher_args.json)')
    parser.add_argument('--out', help='write the manifest to this file')
    parser.add_argument('--bundle', help='write the combined flash image to this file')
    parser.add_argument('--check', metavar='MANIFEST',
                        help='re-hash the build against a reviewed manifest and refuse on mismatch')
    parser.add_argument('--verify', metavar='BUNDLE',
                        help='verify a (prebuilt) bundle against the manifest bundle_sha256 header')
    args = parser.parse_args()

    if args.verify:
        if not args.check:
            parser.error('--verify needs --check MANIFEST (the manifest carrying bundle_sha256)')
        verify_bundle(args.verify, Path(args.check))
        return
    if args.check:
        if not args.build:
            parser.error('--check needs --build (or --verify BUNDLE for a prebuilt image)')
        check_manifest(Path(args.build), Path(args.check))
        return
    if not args.build:
        parser.error('--build is required (to emit a manifest or a bundle)')
    build = Path(args.build)

    bundle_sha256 = None
    if args.bundle:
        mapped = load_flash_map(build)
        length = write_bundle(build, mapped, Path(args.bundle))
        bundle_sha256 = sha256_of(Path(args.bundle))
        print(f'bundle={args.bundle} size={length} sha256={bundle_sha256}')
    emit_manifest(build, args.out, bundle_sha256=bundle_sha256)


if __name__ == '__main__':
    main()
