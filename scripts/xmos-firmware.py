#!/usr/bin/env python3
"""XMOS flash readback, backup, and restore for the XVF3800 microphone-array MCU.

This is the checkpoint tooling the board gets before anyone touches its MCU flash: read the flash
back into a validated image, keep the image and a record of what it is, and be able to prove --
before a write -- exactly what a restore would do.

It is deliberately read-only as it ships. `backup` and `check` only ever read and only ever write
to disk. `restore` shows the exact bytes and offset it would write and refuses to actually write:
rewriting a microphone-array MCU is a device change that needs separate authorization, and a tool
that "backs up and restores" in one motion is how a bad write overwrites a good board with nothing
on file to undo it. That refusal is a feature and is covered by the host tests.

The driver is injectable so the host can prove every path with a fake flash and never need the
XMOS toolchain or the board: the tests pass a driver that returns canned bytes, and no real `xjtag`
is ever invoked. Without `--connect`, no device is contacted at all.

Usage:
    python3 xmos-firmware.py check  --image <dump.img>
    python3 xmos-firmware.py backup --image <dump.img> [--label ...] [--out <dir>]
    python3 xmos-firmware.py backup --connect --tool xjtag --chip <name> [--label ...] [--out <dir>]
    python3 xmos-firmware.py restore --image <dump.img> [--record <record.json>]   # dry run only
    python3 xmos-firmware.py diff   --a <dump.img> --b <dump.img>
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xmos_checkpoint import (build_checkpoint, parse_checkpoint, record_for,  # noqa: E402
                             diff_checkpoints)

#: The driver is a callable that returns the raw flash bytes. None means "build one from --tool".
#: Keeping it a plain callable (not a class) is what lets the tests inject a fake with no imports.


def build_driver(tool: str, chip: str):
    """The real driver: ask the XMOS tooling to dump the flash. Left thin on purpose -- the
    checkpoint, the validation, and the restore guard live here, not in how a hex file is fetched,
    so a different way of reading the chip changes nothing about what is trusted."""
    def read_flash() -> bytes:
        # The tool dumps hex lines; read until it stops producing. A timeout is a hard stop so a
        # wedged JTAG session cannot hang the checkpoint forever.
        read = subprocess.run([tool, 'read', '--device', chip], capture_output=True, text=True,
                              timeout=300)
        if read.returncode != 0:
            raise RuntimeError(f'{tool} read failed: '
                               f'{(read.stderr or read.stdout or "").strip()[:120]}')
        chunks = [bytes.fromhex(line.strip()) for line in (read.stdout or '').splitlines()
                  if line.strip()]
        if not chunks:
            raise RuntimeError(f'{tool} read produced no data for {chip}')
        return b''.join(chunks)
    return read_flash


def wrap_and_save(image: bytes, label: str, out: Path, driver: str) -> dict:
    """Build the validated checkpoint, verify it reads back as we built it (the round-trip a
    corrupted write would break), and write the image plus its record next to it."""
    checkpoint = build_checkpoint(image)
    # Round-trip before saving: re-wrapping the flash the image actually stores must reproduce the
    # exact bytes, which is what a corrupted or truncated write would break. (The stored flash is
    # page-padded, so this is not the raw input's hash -- it is the envelope's self-consistency.)
    parsed = parse_checkpoint(checkpoint)
    assert build_checkpoint(parsed['flash']) == checkpoint, 'round-trip mismatch'
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%S')
    image_path = out / f'xmos-{label or "flash"}-{stamp}.img'
    image_path.write_bytes(checkpoint)
    record = record_for(checkpoint, label=label or 'flash', driver=driver, created=stamp)
    record['image'] = image_path.name
    (out / f'xmos-{label or "flash"}-{stamp}.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def restore_dry_run(image: bytes, record: Path | None) -> int:
    """Show what a restore would do and stop there. The refusal is the contract: `--write` is
    accepted so a caller can ask, and answered no, until a separate, explicit authorization is
    added to this command. A restore that writes is a device change, not a backup."""
    parsed = parse_checkpoint(image)
    print('restore (DRY RUN -- no device write):')
    print(f'  flash length : {parsed["flash_len"]} bytes, page {parsed["page_size"]}')
    print(f'  image sha256 : {parsed["sha256"]}')
    if record:
        expected = json.loads(record.read_text())
        if expected.get('sha256') != parsed['sha256']:
            raise SystemExit(f'record {record} (sha256={expected.get("sha256")}) does not match '
                             f'this image (sha256={parsed["sha256"]}); refusing to restore from it')
        print(f'  record matches: {record.name} (label={expected.get("label")}, '
              f'created={expected.get("created")})')
    print('  would write    : the full image to the MCU flash at offset 0x0')
    print('  NOT WRITEN: MCU rewrite requires separate authorization; this tool is read-only.')
    return 0


def main(argv: list[str] | None = None, driver=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['check', 'backup', 'restore', 'diff'])
    parser.add_argument('--image', type=Path, help='a raw flash dump or a stored checkpoint image')
    parser.add_argument('--a', type=Path)
    parser.add_argument('--b', type=Path)
    parser.add_argument('--label', default='')
    parser.add_argument('--out', type=Path, default=Path('xmos-checkpoints'))
    parser.add_argument('--record', type=Path, help='record json to verify a restore against')
    parser.add_argument('--connect', action='store_true',
                        help='contact a device (with --tool); without it nothing is connected')
    parser.add_argument('--tool', default='xjtag', help='the XMOS read tool')
    parser.add_argument('--chip', default='', help='the chip/device name to read')
    parser.add_argument('--write', action='store_true',
                        help='accepted and refused: this tool never writes to the MCU')
    arguments = parser.parse_args(argv)

    if arguments.command == 'check':
        if not arguments.image:
            raise SystemExit('check needs --image')
        image = arguments.image.read_bytes()
        record = record_for(image, label=arguments.label or 'check')
        print(json.dumps(record, indent=2))
        return 0

    if arguments.command == 'diff':
        if not (arguments.a and arguments.b):
            raise SystemExit('diff needs --a and --b')
        result = diff_checkpoints(arguments.a.read_bytes(), arguments.b.read_bytes())
        if result['identical']:
            print('identical')
        else:
            print(f'differing: {len(result["hunks"])} hunk(s)')
            for start, end in result['hunks']:
                print(f'  0x{start:06x}-0x{end:06x} ({end - start} bytes)')
        return 0

    if arguments.command == 'backup':
        if arguments.connect:
            if not arguments.chip:
                raise SystemExit('backup --connect needs --chip')
            read = driver if driver is not None else build_driver(arguments.tool, arguments.chip)
            driver_name = 'injected' if driver is not None else arguments.tool
            print(f'contacting {arguments.chip} via {driver_name} ...')
            raw = read()
        elif arguments.image:
            raw = arguments.image.read_bytes()
            driver_name = 'file'
        else:
            raise SystemExit('backup needs --image, or --connect --chip (and it is read-only)')
        record = wrap_and_save(raw, arguments.label, arguments.out, driver_name)
        print(f'saved {record["image"]}: {record["flash_len"]} bytes, sha256={record["sha256"][:16]}...')
        return 0

    if arguments.command == 'restore':
        if arguments.write:
            raise SystemExit('restore --write is refused: this tool is read-only. Rewriting the '
                             'MCU needs separate authorization and a separate command.')
        if not arguments.image:
            raise SystemExit('restore needs --image')
        return restore_dry_run(arguments.image.read_bytes(), arguments.record)

    raise SystemExit(f'unknown command {arguments.command}')


if __name__ == '__main__':
    raise SystemExit(main())
