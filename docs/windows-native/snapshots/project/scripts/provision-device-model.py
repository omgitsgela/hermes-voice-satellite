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
VERSION = 2
VERSION_LEGACY = 1
NAME_MAX = 64
SHA_LEN = 32
HEADER_SIZE_V1 = 108      # 4 + 2 + 2 + 64 + 4 + 32
POLICY_SIZE = 12          # float32 + uint16 + uint16 + uint32
HEADER_SIZE = HEADER_SIZE_V1 + POLICY_SIZE   # 120

# The scorer's own bounds (wy_mww_scorer.cpp / wy_model.c). A manifest outside
# them is refused here rather than written into a blob the device would reject.
CUTOFF_MIN, CUTOFF_MAX = 0.01, 1.0
WINDOW_MIN, WINDOW_MAX = 1, 32
STEP_MIN, STEP_MAX = 1, 100
ARENA_MIN, ARENA_MAX = 1024, 1048576


def load_manifest(path: Path) -> dict:
    """The `micro` block of a microWakeWord model's JSON companion.

    A model is provisioned as a unit: the .tflite plus the numbers it was
    trained to be scored with. The companion is the model's own
    `<stem>.json`, which the model library publishes beside it. A model with no
    companion is provisioned as version 1 and the firmware's compiled defaults
    apply, which is honest rather than guessing at a policy.
    """
    import json
    manifest_path = path.with_suffix('.json')
    if not manifest_path.exists():
        return {}
    data = json.loads(manifest_path.read_text())
    micro = data.get('micro')
    if not isinstance(micro, dict):
        raise ValueError('%s has no `micro` block' % manifest_path)
    policy = {
        'probability_cutoff': float(micro['probability_cutoff']),
        'sliding_window_size': int(micro['sliding_window_size']),
        'feature_step_size': int(micro['feature_step_size']),
        'tensor_arena_size': int(micro['tensor_arena_size']),
    }
    validate_policy(policy)
    return policy


def validate_policy(policy: dict) -> None:
    if not CUTOFF_MIN <= policy['probability_cutoff'] <= CUTOFF_MAX:
        raise ValueError('probability_cutoff out of range: %r' % policy['probability_cutoff'])
    if not WINDOW_MIN <= policy['sliding_window_size'] <= WINDOW_MAX:
        raise ValueError('sliding_window_size out of range: %r' % policy['sliding_window_size'])
    if not STEP_MIN <= policy['feature_step_size'] <= STEP_MAX:
        raise ValueError('feature_step_size out of range: %r' % policy['feature_step_size'])
    if not ARENA_MIN <= policy['tensor_arena_size'] <= ARENA_MAX:
        raise ValueError('tensor_arena_size out of range: %r' % policy['tensor_arena_size'])


def build_header(name: bytes, data_len: int, sha256: bytes, policy: dict | None = None) -> bytes:
    if not 0 < len(name) < NAME_MAX:
        raise ValueError('model name must be 1..%d bytes (the NUL is ours)'
                         % (NAME_MAX - 1))
    if data_len == 0:
        raise ValueError('a model with no bytes is not a model')
    if len(sha256) != SHA_LEN:
        raise ValueError('sha256 must be %d bytes' % SHA_LEN)
    name_b = name[:NAME_MAX - 1].ljust(NAME_MAX, b'\x00')
    if policy is not None:
        validate_policy(policy)
    version = VERSION if policy is not None else VERSION_LEGACY
    header = (struct.pack('<IHH', MAGIC, version, 0) + name_b
              + struct.pack('<I', data_len) + sha256)
    if policy is not None:
        header += struct.pack('<fHHI',
                              policy['probability_cutoff'],
                              policy['sliding_window_size'],
                              policy['feature_step_size'],
                              policy['tensor_arena_size'])
    return header


def build_blob(model_bytes: bytes, name: bytes, policy: dict | None = None) -> bytes:
    return build_header(name, len(model_bytes),
                        hashlib.sha256(model_bytes).digest(), policy) + model_bytes


def verify_blob(blob: bytes, lib) -> tuple:
    """Verify a staged blob with the PRODUCTION parser (wy_model.c) and the
    provisioner's own SHA-256. Returns (name, data_len, sha_ok, policy) where
    policy is the version 2 inference policy (or None for a version 1 blob).
    Raises on a structural failure."""
    H_ = lib.wy_model_header_parse
    # ctypes struct mirroring wy_model_header (v2: the policy follows sha256)
    import ctypes as C
    class S(C.Structure):
        _fields_ = [('magic', C.c_uint32), ('version', C.c_uint16),
                    ('reserved', C.c_uint16), ('name', C.c_char * NAME_MAX),
                    ('data_len', C.c_uint32), ('sha256', C.c_uint8 * SHA_LEN),
                    ('probability_cutoff', C.c_float),
                    ('sliding_window_size', C.c_uint16),
                    ('feature_step_size', C.c_uint16),
                    ('tensor_arena_size', C.c_uint32)]
    h = S()
    buf = C.create_string_buffer(len(blob))
    C.memmove(buf, blob, len(blob))
    rc = H_(buf, len(blob), C.byref(h))
    if rc != 0:
        raise ValueError('production parser rejected the blob (rc=%d)' % rc)
    header_len = HEADER_SIZE if h.version == VERSION else HEADER_SIZE_V1
    model = blob[header_len:header_len + h.data_len]
    sha_ok = hashlib.sha256(model).digest() == bytes(h.sha256)
    policy = None
    if h.version == VERSION:
        policy = {
            'probability_cutoff': round(float(h.probability_cutoff), 4),
            'sliding_window_size': int(h.sliding_window_size),
            'feature_step_size': int(h.feature_step_size),
            'tensor_arena_size': int(h.tensor_arena_size),
        }
    return h.name.rstrip(b'\x00').decode(), h.data_len, sha_ok, policy


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
    # Windows cannot reopen a NamedTemporaryFile while its creator holds it open.
    # Close the staged file before esptool opens it; the directory owns cleanup.
    with tempfile.TemporaryDirectory(prefix='satellite-model-') as directory:
        staged = Path(directory) / 'model.bin'
        staged.write_bytes(blob)
        cmd += ['--port', port, '--baud', str(baud), 'write_flash',
                '--flash_size', '8MB', str(MODEL_OFFSET), str(staged)]
        print('esptool: %s' % ' '.join(cmd))
        subprocess.run(cmd, check=True, timeout=180)


def self_test() -> int:
    import random
    random.seed(0xA11CE)
    model = bytes(random.getrandbits(8) for _ in range(4096))
    name = b'hey_hermes'
    policy = load_manifest(ROOT / 'firmware/esp32/models/hey_hermes.tflite')
    blob = build_blob(model, name, policy)
    assert len(blob) == HEADER_SIZE + len(model), len(blob)

    lib = load_model_lib()
    parsed_name, data_len, sha_ok, parsed_policy = verify_blob(blob, lib)
    assert parsed_name == name.decode(), parsed_name
    assert data_len == len(model), data_len
    assert sha_ok, 'sha256 mismatch against the production parser'
    assert parsed_policy == {k: round(v, 4) if isinstance(v, float) else v
                             for k, v in policy.items()}, parsed_policy

    # A version 1 blob (model without a manifest) is still accepted, and it
    # carries no policy: the firmware's compiled defaults apply.
    legacy = build_blob(model, name)
    assert len(legacy) == HEADER_SIZE_V1 + len(model), len(legacy)
    _, _, sha_ok, parsed_policy = verify_blob(legacy, lib)
    assert sha_ok and parsed_policy is None, 'a v1 blob must parse with no policy'

    # A corrupt byte in the model region breaks the hash (the glue would
    # reject it even though the header parses).
    bad = bytearray(blob)
    bad[-1] ^= 0xFF
    try:
        _, _, sha_ok, _ = verify_blob(bytes(bad), lib)
        assert not sha_ok, 'corrupted model must fail the hash'
    except ValueError:
        pass  # a header-region corruption fails structure; either way rejected

    # A truncated blob is rejected by the production parser.
    try:
        verify_blob(blob[:HEADER_SIZE - 1], lib)
        raise AssertionError('a truncated header must be rejected')
    except ValueError:
        pass

    # A policy the scorer would refuse is refused here, before it is written.
    for broken in ({**policy, 'probability_cutoff': 0.0},
                   {**policy, 'sliding_window_size': 0},
                   {**policy, 'tensor_arena_size': 16}):
        try:
            build_blob(model, name, broken)
            raise AssertionError('an out-of-range policy must be refused: %r' % broken)
        except ValueError:
            pass

    print('self-test: build_blob + production-parser verify: OK')
    print('  v2 blob %d bytes (header %d + model %d), name %r, policy %r'
          % (len(blob), HEADER_SIZE, len(model), parsed_name, policy))
    print('  v1 blob %d bytes (header %d) still accepted, no policy' % (len(legacy), HEADER_SIZE_V1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Provision the wake model blob onto the `model` partition.')
    parser.add_argument('--model', help='the model file (e.g. hey-hermes.tflite)')
    parser.add_argument('--name', help='the config wake_word (model selector), '
                                       'e.g. hey_hermes')
    parser.add_argument('--params-json',
                        help='the model manifest to take the inference policy from '
                             '(default: <model>.json beside the model, if present)')
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
        name, data_len, sha_ok, policy = verify_blob(blob, lib)
        print('blob OK: name=%r data_len=%d sha256=%s policy=%r'
              % (name, data_len, sha_ok, policy))
        return 0 if sha_ok else 1

    if not args.model:
        parser.error('one of --self-test, --verify-only, or --model is required')
    model = Path(args.model).read_bytes()
    if not args.name:
        parser.error('--name (the model selector) is required')
    if args.params_json:
        policy = load_manifest(Path(args.params_json))
        if not policy:
            parser.error('%s carries no `micro` block' % args.params_json)
    else:
        policy = load_manifest(Path(args.model)) or None
    blob = build_blob(model, args.name.encode(), policy)
    print('built blob: %d bytes (header %d + model %d), name=%r, policy=%s'
          % (len(blob), HEADER_SIZE if policy else HEADER_SIZE_V1, len(model),
             args.name, 'model manifest' if policy else 'none (firmware defaults)'))
    if policy:
        print('  cutoff=%.2f window=%d step=%dms arena=%dB'
              % (policy['probability_cutoff'], policy['sliding_window_size'],
                 policy['feature_step_size'], policy['tensor_arena_size']))
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
