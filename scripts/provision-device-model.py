#!/usr/bin/env python3
"""Provision the wake model blob onto the `model` data partition.

Usage:
  provision-device-model.py --model <file> --name <name> --output <staged blob file>
  provision-device-model.py --model <file> --name <name> --port /dev/ttyUSB0 [--baud 460800]
  provision-device-model.py --output <staged blob file> --verify-only
  provision-device-model.py --self-test

The blob is exactly the format wy_model.h documents (magic "WKM1", version,
model name, data length, SHA-256, then the model bytes) and is written to the
whole `model` partition -- a raw esp_partition_read region, NOT a mounted
filesystem, the same raw-store pattern the TLS credentials use (label `tls`,
partition `storage`). `--output` stages the blob to a file so it can be
reviewed/committed-to-flash-tooling; `--port` writes it straight to the board
with esptool (erase-write of the partition range ONLY: this script never
touches any other region, and a full-chip erase is never issued).

The model NAME is the config `wake_word` -- the model selector, e.g.
"hey_hermes". It is deliberately distinct from the session core's detection
name (the "hermes" the wake emits on fire): the model is selected by what it
was trained to hear, the detection event is what the pipeline knows.

--self-test builds a synthetic model, writes it to a temp blob, and verifies
it with the PRODUCTION parser (wy_model.c compiled on the host, the same
source that ships): a cross-language oracle that the provisioner emits
exactly what the firmware accepts.
"""
import argparse
import hashlib
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / 'firmware/esp32/components'
MODEL_PARTITION = 'model'
MODEL_OFFSET = 0x610000   # partitions.csv
MAGIC = 0x314D4B57        # "WKM1" little-endian
VERSION = 1
NAME_MAX = 64
SHA_LEN = 32
HEADER_SIZE = 108         # 4 + 2 + 2 + 64 + 4 + 32


def build_header(name: bytes, data_len: int, sha256: bytes) -> bytes:
    if not 0 < len(name) < NAME_MAX:
        raise ValueError('model name must be 1..%d bytes (the NUL is ours)'
                         % (NAME_MAX - 1))
    if data_len == 0:
        raise ValueError('a model with no bytes is not a model')
    if len(sha256) != SHA_LEN:
        raise ValueError('sha256 must be %d bytes' % SHA_LEN)
    name_b = name[:NAME_MAX - 1].ljust(NAME_MAX, b'\x00')
    return (struct.pack('<IHH', MAGIC, VERSION, 0) + name_b
            + struct.pack('<I', data_len) + sha256)


def build_blob(model_bytes: bytes, name: bytes) -> bytes:
    return build_header(name, len(model_bytes),
                        hashlib.sha256(model_bytes).digest()) + model_bytes


def verify_blob(blob: bytes, lib) -> tuple:
    """Verify a staged blob with the PRODUCTION parser (wy_model.c) and the
    provisioner's own SHA-256. Returns (name, data_len, sha_ok). Raises on a
    structural failure."""
    H = type('H', (), {})
    H_ = lib.wy_model_header_parse
    # ctypes struct mirroring wy_model_header
    import ctypes as C
    class S(C.Structure):
        _fields_ = [('magic', C.c_uint32), ('version', C.c_uint16),
                    ('reserved', C.c_uint16), ('name', C.c_char * NAME_MAX),
                    ('data_len', C.c_uint32), ('sha256', C.c_uint8 * SHA_LEN)]
    h = S()
    buf = C.create_string_buffer(len(blob))
    C.memmove(buf, blob, len(blob))
    rc = H_(buf, len(blob), C.byref(h))
    if rc != 0:
        raise ValueError('production parser rejected the blob (rc=%d)' % rc)
    model = blob[HEADER_SIZE:HEADER_SIZE + h.data_len]
    sha_ok = hashlib.sha256(model).digest() == bytes(h.sha256)
    return h.name.rstrip(b'\x00').decode(), h.data_len, sha_ok


def load_model_lib() -> 'object':
    import ctypes as C
    src = COMP / 'wy_wake/wy_model.c'
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'm.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-shared', '-fPIC', '-I' + str(COMP / 'wy_wake'),
                        str(src), '-o', str(out)], check=True)
        lib = C.CDLL(str(out))
        lib.wy_model_header_parse.argtypes = [C.c_void_p, C.c_size_t, C.c_void_p]
        lib.wy_model_header_parse.restype = C.c_int
        return lib


def flash_port(port: str, baud: int, blob: bytes) -> None:
    esptool = shutil.which('esptool.py')
    if not esptool:
        # The IDF-managed esptool lives on PATH only inside the IDF shell;
        # fall back to the Python module form.
        cmd = [sys.executable, '-m', 'esptool']
    else:
        cmd = [esptool]
    with tempfile.NamedTemporaryFile(suffix='.bin') as f:
        f.write(blob)
        f.flush()
        cmd += ['--port', port, '--baud', str(baud), 'write_flash',
                '--flash_size', '8MB', str(MODEL_OFFSET), f.name]
        print('esptool: %s' % ' '.join(cmd))
        subprocess.run(cmd, check=True)


def self_test() -> int:
    import random
    random.seed(0xA11CE)
    model = bytes(random.getrandbits(8) for _ in range(4096))
    name = b'hey_hermes'
    blob = build_blob(model, name)
    assert len(blob) == HEADER_SIZE + len(model)

    lib = load_model_lib()
    parsed_name, data_len, sha_ok = verify_blob(blob, lib)
    assert parsed_name == name.decode(), parsed_name
    assert data_len == len(model), data_len
    assert sha_ok, 'sha256 mismatch against the production parser'

    # A corrupt byte in the model region breaks the hash (the glue would
    # reject it even though the header parses).
    bad = bytearray(blob)
    bad[-1] ^= 0xFF
    try:
        _, _, sha_ok = verify_blob(bytes(bad), lib)
        assert not sha_ok, 'corrupted model must fail the hash'
    except ValueError:
        pass  # a header-region corruption fails structure; either way rejected

    # A truncated blob is rejected by the production parser.
    try:
        verify_blob(blob[:HEADER_SIZE - 1], lib)
        raise AssertionError('a truncated header must be rejected')
    except ValueError:
        pass

    print('self-test: build_blob + production-parser verify: OK')
    print('  blob %d bytes (header %d + model %d), name %r, sha256 ok'
          % (len(blob), HEADER_SIZE, len(model), parsed_name))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Provision the wake model blob onto the `model` partition.')
    parser.add_argument('--model', help='the model file (e.g. hey-hermes.tflite)')
    parser.add_argument('--name', help='the config wake_word (model selector), '
                                       'e.g. hey_hermes')
    parser.add_argument('--output', help='stage the blob to this file')
    parser.add_argument('--port', help='flash it straight to this serial port')
    parser.add_argument('--baud', type=int, default=460800)
    parser.add_argument('--verify-only', action='store_true',
                        help='verify an existing staged blob (with --output)')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if args.verify_only:
        if not args.output:
            parser.error('--verify-only needs --output')
        blob = Path(args.output).read_bytes()
        lib = load_model_lib()
        name, data_len, sha_ok = verify_blob(blob, lib)
        print('blob OK: name=%r data_len=%d sha256=%s' % (name, data_len, sha_ok))
        return 0 if sha_ok else 1

    if not args.model:
        parser.error('one of --self-test, --verify-only, or --model is required')
    model = Path(args.model).read_bytes()
    if not args.name:
        parser.error('--name (the model selector) is required')
    blob = build_blob(model, args.name.encode())
    print('built blob: %d bytes (header %d + model %d), name=%r'
          % (len(blob), HEADER_SIZE, len(model), args.name))
    if args.output:
        out = Path(args.output)
        out.write_bytes(blob)
        print('staged: %s' % out)
        lib = load_model_lib()
        name, data_len, sha_ok = verify_blob(blob, lib)
        if not sha_ok or name != args.name or data_len != len(model):
            print('STAGED BLOB FAILED VERIFICATION', file=sys.stderr)
            return 1
        print('staged blob verified against the production parser: OK')
    if args.port:
        print('writing %s -> %s at 0x%06x (partition %s; no erase, no other '
              'regions touched)' % (len(blob), args.port, MODEL_OFFSET,
                                    MODEL_PARTITION))
        flash_port(args.port, args.baud, blob)
    if not args.output and not args.port:
        parser.error('give --output, --port, or --verify-only')
    return 0


if __name__ == '__main__':
    sys.exit(main())
