#!/usr/bin/env python3
"""The XMOS flash checkpoint record: one implementation, used by the tool and the tests.

The XVF3800's microphone array runs on an XMOS MCU whose flash we can read back and, with separate
authorization, rewrite. The risk that makes a checkpoint worth having is the one that erases a
working board: a bad write with nothing on file to put it back. So before any write, the current
flash is read back into a validated image, and "can I restore this?" is answered by the bytes rather
than by hope.

This module is the format and its validation, kept pure so the host can pin it down without a
board. scripts/xmos-firmware.py imports it to build and check images, and the host tests import it
to say what an intact or a corrupted image should do, so a change to the layout cannot be made on
one side only.

Layout (little-endian): a fixed 24-byte header, then the raw flash, page-aligned and 0xFF-padded.

    offset  size  field
    0       8     magic, b"HVXMC01\\x00"
    8       2     version (1)
    10      2     header length (24), so a format change is caught rather than misread
    12      4     flash length in bytes, the padded length actually stored after the header
    16      4     page size the dump is aligned to (informational; the stored length is a multiple)
    20      4     CRC-32 (IEEE) over the stored flash bytes
"""
import hashlib
import struct
import zlib

MAGIC = b'HVXMC01\x00'
VERSION = 1
HEADER_LEN = 24
DEFAULT_PAGE = 256

_HEADER = struct.Struct('<8sHHIII')
assert _HEADER.size == HEADER_LEN, _HEADER.size


def build_checkpoint(flash: bytes, page_size: int = DEFAULT_PAGE) -> bytes:
    """Wrap a raw flash dump in the validated envelope. 0xFF-pads to the page size, so the stored
    image length is deterministic for a given flash and the padding is indistinguishable from real
    unwritten flash (what an erased XMOS flash byte reads as)."""
    if page_size <= 0:
        raise ValueError('page size must be positive')
    padded = _pad(flash, page_size)
    crc = zlib.crc32(padded) & 0xFFFFFFFF
    header = _HEADER.pack(MAGIC, VERSION, HEADER_LEN, len(padded), page_size, crc)
    return header + padded


def parse_checkpoint(image: bytes) -> dict:
    """The same validation the tool performs on a stored image: reject a truncated, wrong-version,
    or CRC-corrupt image rather than misreading it. Returns the declared fields plus the stored
    flash and its SHA-256, which is what a record keys on."""
    if len(image) < HEADER_LEN:
        raise ValueError('image is shorter than the header')
    magic, version, header_len, flash_len, page_size, crc = _HEADER.unpack(image[:HEADER_LEN])
    if magic != MAGIC:
        raise ValueError('bad magic')
    if version != VERSION:
        raise ValueError(f'unsupported version {version}')
    if header_len != HEADER_LEN:
        raise ValueError(f'header length {header_len} is not {HEADER_LEN}')
    if page_size <= 0:
        raise ValueError('page size is not positive')
    stored = len(image) - HEADER_LEN
    if stored != flash_len:
        raise ValueError(f'stored length {stored} does not match declared flash length {flash_len}')
    if flash_len % page_size:
        raise ValueError('stored flash is not page aligned')
    flash = image[HEADER_LEN:]
    if zlib.crc32(flash) & 0xFFFFFFFF != crc:
        raise ValueError('CRC mismatch')
    return {'version': version, 'flash_len': flash_len, 'page_size': page_size,
            'flash': flash, 'sha256': hashlib.sha256(flash).hexdigest()}


def record_for(image: bytes, *, label: str, driver: str = '', created: str = '') -> dict:
    """The record that goes next to an image: its identity (what a later restore or verify keys on)
    plus the provenance. The sha256 is of the stored flash, not the whole image, so two images of
    the same flash built at different times compare equal on what matters."""
    parsed = parse_checkpoint(image)
    return {'label': label, 'driver': driver, 'created': created,
            'flash_len': parsed['flash_len'], 'page_size': parsed['page_size'],
            'sha256': parsed['sha256'], 'image_sha256': hashlib.sha256(image).hexdigest(),
            'image_bytes': len(image), 'version': parsed['version']}


def diff_checkpoints(image_a: bytes, image_b: bytes) -> dict:
    """Whether two readbacks are the same board, and if not, where they diverge. This is the
    "has it drifted since the backup" answer: identical is the go case, and a hunk list says
    exactly which byte ranges changed rather than hiding the size of the difference."""
    a = parse_checkpoint(image_a)
    b = parse_checkpoint(image_b)
    if a['sha256'] == b['sha256']:
        return {'identical': True, 'hunks': []}
    fa, fb = a['flash'], b['flash']
    length = min(len(fa), len(fb))
    hunks = []
    start = None
    for i in range(length):
        if fa[i] != fb[i]:
            if start is None:
                start = i
        elif start is not None:
            hunks.append([start, i])
            start = None
    if start is not None:
        hunks.append([start, length])
    if len(fa) != len(fb):
        hunks.append([min(length, max(len(fa), len(fb))), max(len(fa), len(fb))])
    return {'identical': False, 'hunks': hunks}


def _pad(flash: bytes, page_size: int) -> bytes:
    remainder = len(flash) % page_size
    if remainder:
        return flash + b'\xff' * (page_size - remainder)
    return flash
