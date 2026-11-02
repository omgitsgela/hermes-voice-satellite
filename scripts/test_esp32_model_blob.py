#!/usr/bin/env python3
"""The wake model blob's pure layout logic (wy_model.c), host-tested.

The blob is the file format of the `model` data partition (D3): one header
(magic, version, model name, data length, SHA-256) followed -- from version 2 --
by the model's own inference policy and then the model bytes. This module owns
only the hash-free layout logic -- the provisioner computes the SHA-256 (in
Python), the device glue re-checks it (mbedTLS) -- so the exact production
source compiles on the host with -Werror and is tested directly. A blob that
fails any check must be rejected whole: the detector falls back to its no-model
state, it never runs a garbage scorer.

Two header versions are exercised here because both are read on the device: a
device provisioned before the policy moved into the blob carries version 1 and
must keep waking after a firmware update, so the v1 path is pinned too.
"""
import ctypes as C
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / 'firmware/esp32/components'

MAGIC = 0x314D4B57          # "WKM1" little-endian
VERSION = 2
VERSION_LEGACY = 1
NAME_MAX = 64
SHA_LEN = 32
POLICY_SIZE = 12
HEADER_SIZE_V1 = 4 + 2 + 2 + NAME_MAX + 4 + SHA_LEN   # 108
HEADER_SIZE = HEADER_SIZE_V1 + POLICY_SIZE            # 120

# The bundled default model's own policy (models/hey_hermes.json).
POLICY = {
    'probability_cutoff': 0.5,
    'sliding_window_size': 5,
    'feature_step_size': 10,
    'tensor_arena_size': 32000,
}


class ModelBlobTests(unittest.TestCase):
    def setUp(self):
        self._keepalive = []

    def library(self):
        source = COMP / 'wy_wake/wy_model.c'
        self.assertTrue(source.exists(), 'model module missing: %s' % source)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'model.so'
        subprocess.run(
            ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
             '-I' + str(COMP / 'wy_wake'),
             str(source), '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        Policy = type('Policy', (C.Structure,), {
            '_fields_': [('probability_cutoff', C.c_float),
                         ('sliding_window_size', C.c_uint16),
                         ('feature_step_ms', C.c_uint16),
                         ('tensor_arena_size', C.c_uint32)]})
        self.Policy = Policy
        self._keepalive.append(Policy)
        lib.wy_model_header_pack.argtypes = [C.c_char_p, C.c_uint32,
                                             C.POINTER(C.c_uint8),
                                             C.POINTER(Policy),
                                             C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
        lib.wy_model_header_pack.restype = C.c_int
        lib.wy_model_header_parse.argtypes = [C.c_void_p, C.c_size_t, C.c_void_p]
        lib.wy_model_header_parse.restype = C.c_int
        lib.wy_model_header_peek.argtypes = [C.c_void_p, C.c_size_t, C.c_void_p]
        lib.wy_model_header_peek.restype = C.c_int
        return lib

    def header_struct(self, lib):
        class H(C.Structure):
            _fields_ = [('magic', C.c_uint32), ('version', C.c_uint16),
                        ('reserved', C.c_uint16), ('name', C.c_char * NAME_MAX),
                        ('data_len', C.c_uint32), ('sha256', C.c_uint8 * SHA_LEN),
                        ('probability_cutoff', C.c_float),
                        ('sliding_window_size', C.c_uint16),
                        ('feature_step_ms', C.c_uint16),
                        ('tensor_arena_size', C.c_uint32)]
        self._keepalive.append(H)
        return H

    def policy_struct(self, policy=POLICY):
        return self.Policy(float(policy['probability_cutoff']),
                           int(policy['sliding_window_size']),
                           int(policy['feature_step_size']),
                           int(policy['tensor_arena_size']))

    def pack(self, lib, name, data_len, sha, policy=POLICY):
        size = HEADER_SIZE if policy is not None else HEADER_SIZE_V1
        out = C.create_string_buffer(size)
        out_len = C.c_size_t(0)
        rc = lib.wy_model_header_pack(
            name, data_len, (C.c_uint8 * SHA_LEN)(*sha),
            C.byref(self.policy_struct(policy)) if policy is not None else None,
            out, size, C.byref(out_len))
        return rc, bytes(out.raw[:size]), out_len.value

    def parse(self, lib, blob):
        H = self.header_struct(lib)
        h = H()
        buf = C.create_string_buffer(len(blob))
        C.memmove(buf, blob, len(blob))
        rc = lib.wy_model_header_parse(buf, len(blob), C.byref(h))
        return rc, h

    def peek(self, lib, header_bytes):
        H = self.header_struct(lib)
        h = H()
        buf = C.create_string_buffer(len(header_bytes))
        C.memmove(buf, header_bytes, len(header_bytes))
        rc = lib.wy_model_header_peek(buf, len(header_bytes), C.byref(h))
        return rc, h

    def good_blob(self, name=b'hey_hermes', data_len=2048, sha=None,
                  model_bytes=None, policy=POLICY):
        """The canonical layout this module must round-trip: the exact bytes a
        provisioner emits (header + the model bytes it describes) and the
        device reads back. `policy=None` emits a version 1 header.
        `model_bytes` defaults to data_len filler bytes."""
        if sha is None:
            sha = bytes(range(SHA_LEN))
        name_b = name[:NAME_MAX - 1].ljust(NAME_MAX, b'\0')
        version = VERSION if policy is not None else VERSION_LEGACY
        header = (struct.pack('<IHH', MAGIC, version, 0) + name_b
                  + struct.pack('<I', data_len) + sha)
        if policy is not None:
            header += struct.pack('<fHHI',
                                  policy['probability_cutoff'],
                                  policy['sliding_window_size'],
                                  policy['feature_step_size'],
                                  policy['tensor_arena_size'])
        if model_bytes is None:
            model_bytes = bytes([(i * 7) & 0xFF for i in range(data_len)])
        return header + model_bytes

    # -- pack -------------------------------------------------------------------
    def test_pack_emits_the_canonical_v2_layout(self):
        lib = self.library()
        rc, raw, out_len = self.pack(lib, b'hey_hermes', 2048, bytes(range(SHA_LEN)))
        self.assertEqual(rc, 0)
        self.assertEqual(out_len, HEADER_SIZE)
        # Pack writes the HEADER only; the model bytes follow it on the wire.
        self.assertEqual(raw, self.good_blob()[:HEADER_SIZE])

    def test_pack_without_a_policy_emits_a_v1_header(self):
        lib = self.library()
        rc, raw, out_len = self.pack(lib, b'hey_hermes', 2048,
                                     bytes(range(SHA_LEN)), policy=None)
        self.assertEqual(rc, 0)
        self.assertEqual(out_len, HEADER_SIZE_V1)
        self.assertEqual(raw, self.good_blob(policy=None)[:HEADER_SIZE_V1])

    def test_pack_rejects_short_buffers_and_nulls(self):
        lib = self.library()
        rc, _, _ = self.pack(lib, b'hey_hermes', 2048, bytes(SHA_LEN))
        self.assertEqual(rc, 0)
        out_len = C.c_size_t(0)
        sha = (C.c_uint8 * SHA_LEN)(*bytes(SHA_LEN))
        policy = self.policy_struct()
        # A buffer shorter than the header is a bad argument, not a truncation.
        short = C.create_string_buffer(HEADER_SIZE - 1)
        self.assertEqual(
            lib.wy_model_header_pack(b'hey_hermes', 2048, sha, C.byref(policy),
                                     short, HEADER_SIZE - 1, C.byref(out_len)), -1)
        self.assertEqual(lib.wy_model_header_pack(None, 2048, sha, C.byref(policy),
                                                  C.create_string_buffer(HEADER_SIZE),
                                                  HEADER_SIZE, C.byref(out_len)), -1)
        self.assertEqual(lib.wy_model_header_pack(b'hey_hermes', 2048, None,
                                                  C.byref(policy),
                                                  C.create_string_buffer(HEADER_SIZE),
                                                  HEADER_SIZE, C.byref(out_len)), -1)

    def test_pack_rejects_zero_data_length(self):
        """A model with no bytes is not a model: an empty model region is the
        never-provisioned state, and packing one is a caller bug."""
        lib = self.library()
        rc, _, _ = self.pack(lib, b'hey_hermes', 0, bytes(SHA_LEN))
        self.assertEqual(rc, -1)

    def test_pack_rejects_a_name_that_does_not_fit(self):
        lib = self.library()
        rc, _, _ = self.pack(lib, b'x' * NAME_MAX, 2048, bytes(SHA_LEN))
        self.assertEqual(rc, -1, 'a 64-byte name has no room for the NUL')

    def test_pack_rejects_a_policy_the_scorer_would_refuse(self):
        """A policy out of the scorer's bounds is refused at write time, so a
        blob the device would reject is never staged in the first place."""
        lib = self.library()
        for broken in ({**POLICY, 'probability_cutoff': 0.0},
                       {**POLICY, 'probability_cutoff': 1.5},
                       {**POLICY, 'sliding_window_size': 0},
                       {**POLICY, 'feature_step_size': 0},
                       {**POLICY, 'tensor_arena_size': 16}):
            rc, _, _ = self.pack(lib, b'hey_hermes', 2048, bytes(SHA_LEN), policy=broken)
            self.assertEqual(rc, -1, 'must refuse %r' % broken)

    # -- parse ------------------------------------------------------------------
    def test_parse_round_trips_the_canonical_v2_blob(self):
        lib = self.library()
        blob = self.good_blob()
        rc, h = self.parse(lib, blob)
        self.assertEqual(rc, 0)
        self.assertEqual(h.magic, MAGIC)
        self.assertEqual(h.version, VERSION)
        self.assertEqual(h.reserved, 0)
        self.assertEqual(h.name, b'hey_hermes')
        self.assertEqual(h.data_len, 2048)
        self.assertEqual(bytes(h.sha256), bytes(range(SHA_LEN)))
        self.assertAlmostEqual(h.probability_cutoff, POLICY['probability_cutoff'], places=6)
        self.assertEqual(h.sliding_window_size, POLICY['sliding_window_size'])
        self.assertEqual(h.feature_step_ms, POLICY['feature_step_size'])
        self.assertEqual(h.tensor_arena_size, POLICY['tensor_arena_size'])

    def test_parse_accepts_a_v1_blob_with_no_policy(self):
        """A device provisioned before the policy moved into the blob carries a
        version 1 header. It must keep parsing (the firmware applies its
        compiled defaults), and its policy fields must read as zero so a caller
        can never mistake zeros for a real policy."""
        lib = self.library()
        blob = self.good_blob(policy=None)
        rc, h = self.parse(lib, blob)
        self.assertEqual(rc, 0, 'v1 must stay readable')
        self.assertEqual(h.version, VERSION_LEGACY)
        self.assertEqual(h.name, b'hey_hermes')
        self.assertEqual(h.data_len, 2048)
        self.assertEqual(h.probability_cutoff, 0.0)
        self.assertEqual(h.sliding_window_size, 0)
        self.assertEqual(h.feature_step_ms, 0)
        self.assertEqual(h.tensor_arena_size, 0)

    def test_v1_length_is_measured_against_the_v1_header(self):
        """Regression guard for the class this change introduces: the header
        size depends on the version. A v1 blob whose data_len exactly fills
        108 + data_len must parse; measuring it against the 120-byte v2 header
        would reject a perfectly good provisioned model."""
        lib = self.library()
        blob = self.good_blob(data_len=16, policy=None)
        self.assertEqual(len(blob), HEADER_SIZE_V1 + 16)
        rc, h = self.parse(lib, blob)
        self.assertEqual(rc, 0, 'a v1 blob must not be sized as a v2 one')
        self.assertEqual(h.data_len, 16)
        # The same bytes under a v2 header parse against 120 + data_len.
        blob2 = self.good_blob(data_len=16)
        self.assertEqual(len(blob2), HEADER_SIZE + 16)
        rc, _ = self.parse(lib, blob2)
        self.assertEqual(rc, 0)

    def test_peek_reads_the_header_length_before_the_blob_exists(self):
        """The device loader must learn a model's length BEFORE it reads the model bytes (to size
        its buffer). peek reads the fixed header fields only and does NOT require the blob to hold
        data_len bytes -- so a header-only buffer parses for any length. This is exactly where the
        loader went wrong: it used wy_model_header_parse (which demands the whole blob) on a
        header-only buffer, so every real model failed to parse and was silently discarded. peek
        succeeds on the header alone; parse on that same header alone must still FAIL (that
        whole-blob check is parse's job and is deliberately kept)."""
        lib = self.library()
        header = self.good_blob(data_len=2048)[:HEADER_SIZE]   # header only, no model bytes
        rc, h = self.peek(lib, header)
        self.assertEqual(rc, 0, 'peek must parse a header-only buffer for any length')
        self.assertEqual(h.data_len, 2048)
        self.assertEqual(h.name, b'hey_hermes')
        self.assertAlmostEqual(h.probability_cutoff, POLICY['probability_cutoff'], places=6)
        # The authoritative whole-blob parse still rejects a header-only buffer (by design).
        rc2, _ = self.parse(lib, header)
        self.assertEqual(rc2, -1, 'parse keeps the whole-blob length check')
        # A v1 header is 108 bytes; peek must accept exactly that much for it.
        rc3, h3 = self.peek(lib, self.good_blob(data_len=2048, policy=None)[:HEADER_SIZE_V1])
        self.assertEqual(rc3, 0)
        self.assertEqual(h3.version, VERSION_LEGACY)

    def test_peek_rejects_bad_fields_but_needs_no_blob(self):
        """peek validates the fixed fields (magic, version, reserved, name, and
        a v2 policy) without needing the model bytes: a header-only buffer with
        bad magic or an unterminated name is still rejected, and a name longer
        than its 64-byte field cannot run past the header."""
        lib = self.library()
        header = bytearray(self.good_blob(data_len=2048)[:HEADER_SIZE])
        header[0:1] = b'X'                       # bad magic
        rc, _ = self.peek(lib, bytes(header))
        self.assertEqual(rc, -1)
        header = bytearray(self.good_blob(data_len=2048)[:HEADER_SIZE])
        struct.pack_into('<H', header, 4, 3)     # unknown version
        rc, _ = self.peek(lib, bytes(header))
        self.assertEqual(rc, -1)
        header = bytearray(self.good_blob(data_len=2048)[:HEADER_SIZE])
        header[8:8 + NAME_MAX] = b'x' * NAME_MAX # unterminated name
        rc, _ = self.peek(lib, bytes(header))
        self.assertEqual(rc, -1)
        # A v2 header cut below 120 bytes is not a v2 header.
        rc, _ = self.peek(lib, self.good_blob(data_len=2048)[:HEADER_SIZE - 1])
        self.assertEqual(rc, -1)
        # An out-of-range policy is rejected like any other corrupt field.
        header = bytearray(self.good_blob(data_len=2048)[:HEADER_SIZE])
        struct.pack_into('<f', header, 108, 0.0)  # cutoff 0
        rc, _ = self.peek(lib, bytes(header))
        self.assertEqual(rc, -1)

    def test_parse_leaves_out_untouched_on_any_failure(self):
        """wy_model_header_parse documents "-1 (with *out untouched)" on every failure. A caller
        that parses the next blob into a struct still holding its last good header must not lose
        that good header to a half-validated one. The whole-blob-length failure is the subtle one:
        the fixed fields parse fine (peek fills a temp) but the blob does not hold data_len bytes,
        so the parse must return -1 WITHOUT having written anything to *out."""
        lib = self.library()
        H = self.header_struct(lib)
        out = H()
        # Sentinel header the caller wants preserved if the parse fails.
        out.magic = 0xDEAD
        out.version = 0xBEEF
        out.reserved = 0xFEED
        out.data_len = 0xCAFE
        out.sha256[0] = 0x77
        out.probability_cutoff = 0.25

        def assert_sentinels_intact():
            self.assertEqual(out.magic, 0xDEAD, 'a failed parse wrote magic to *out')
            self.assertEqual(out.version, 0xBEEF, 'a failed parse wrote version to *out')
            self.assertEqual(out.reserved, 0xFEED, 'a failed parse wrote reserved to *out')
            self.assertEqual(out.data_len, 0xCAFE, 'a failed parse wrote data_len to *out')
            self.assertEqual(out.sha256[0], 0x77, 'a failed parse wrote sha256 to *out')
            self.assertEqual(out.probability_cutoff, 0.25, 'a failed parse wrote policy to *out')

        # Case 1: whole-blob-length failure (the regression) -- header claims 2048 bytes, blob
        # holds only 100. Fixed fields are valid, so a naive peek-into-out leaks them into *out.
        blob = self.good_blob(data_len=2048, model_bytes=bytes(100))
        buf = C.create_string_buffer(len(blob))
        C.memmove(buf, blob, len(blob))
        self.assertEqual(lib.wy_model_header_parse(buf, len(blob), C.byref(out)), -1)
        assert_sentinels_intact()

        # Case 2: bad magic (fails before any field is worth keeping).
        blob2 = bytearray(self.good_blob(data_len=16))
        blob2[0:1] = b'X'
        buf2 = C.create_string_buffer(len(blob2))
        C.memmove(buf2, bytes(blob2), len(blob2))
        self.assertEqual(lib.wy_model_header_parse(buf2, len(blob2), C.byref(out)), -1)
        assert_sentinels_intact()

        # Case 3: truncated below the header.
        blob3 = self.good_blob(data_len=16)[:HEADER_SIZE - 1]
        buf3 = C.create_string_buffer(len(blob3))
        C.memmove(buf3, blob3, len(blob3))
        self.assertEqual(lib.wy_model_header_parse(buf3, len(blob3), C.byref(out)), -1)
        assert_sentinels_intact()

        # Case 4: unterminated name.
        blob4 = bytearray(self.good_blob(data_len=16))
        blob4[8:8 + NAME_MAX] = b'x' * NAME_MAX
        buf4 = C.create_string_buffer(len(blob4))
        C.memmove(buf4, bytes(blob4), len(blob4))
        self.assertEqual(lib.wy_model_header_parse(buf4, len(blob4), C.byref(out)), -1)
        assert_sentinels_intact()

        # Case 5: a v2 policy the scorer would refuse.
        blob5 = bytearray(self.good_blob(data_len=16))
        struct.pack_into('<f', blob5, 108, 0.0)
        buf5 = C.create_string_buffer(len(blob5))
        C.memmove(buf5, bytes(blob5), len(blob5))
        self.assertEqual(lib.wy_model_header_parse(buf5, len(blob5), C.byref(out)), -1)
        assert_sentinels_intact()

    def test_parse_rejects_bad_magic(self):
        lib = self.library()
        blob = bytearray(self.good_blob())
        blob[0:1] = b'X'
        rc, _ = self.parse(lib, bytes(blob))
        self.assertEqual(rc, -1)

    def test_parse_rejects_an_unknown_version(self):
        lib = self.library()
        blob = bytearray(self.good_blob())
        struct.pack_into('<H', blob, 4, 3)
        rc, _ = self.parse(lib, bytes(blob))
        self.assertEqual(rc, -1)

    def test_parse_rejects_a_truncated_header(self):
        lib = self.library()
        blob = self.good_blob()
        for cut in (0, 4, 8, HEADER_SIZE_V1, HEADER_SIZE - 1):
            rc, _ = self.parse(lib, blob[:cut])
            self.assertEqual(rc, -1, 'a %d-byte blob is no header' % cut)

    def test_parse_rejects_an_unterminated_name(self):
        """A name with no NUL inside its 64 bytes would make the device read
        past the field: reject it rather than strlen it."""
        lib = self.library()
        blob = bytearray(self.good_blob())
        blob[8:8 + NAME_MAX] = b'x' * NAME_MAX
        rc, _ = self.parse(lib, bytes(blob))
        self.assertEqual(rc, -1)

    def test_parse_rejects_data_len_beyond_the_blob(self):
        """A header claiming more model bytes than the partition read holds is
        corrupt (or a longer model the provisioner never finished writing):
        reject it. A length that EXACTLY fills the remainder is valid, for both
        header versions."""
        lib = self.library()
        for policy, header_size in ((POLICY, HEADER_SIZE), (None, HEADER_SIZE_V1)):
            blob = self.good_blob(data_len=16, policy=policy)
            self.assertEqual(len(blob), header_size + 16)
            rc, h = self.parse(lib, blob)
            self.assertEqual(rc, 0)
            self.assertEqual(h.data_len, 16)
            # Same bytes, but the header's data_len field now claims one byte
            # more than the blob holds: corrupt, reject.
            patched = bytearray(blob)
            struct.pack_into('<I', patched, 72, 17)
            rc, _ = self.parse(lib, bytes(patched))
            self.assertEqual(rc, -1, 'v%d overlong data_len' % (2 if policy else 1))

    def test_parse_rejects_blank_flash(self):
        """An erased (never-provisioned) region reads 0xFF: a valid region is
        one with a real header, so all-0xFF is a no-model state, not a blob."""
        lib = self.library()
        rc, _ = self.parse(lib, b'\xff' * HEADER_SIZE)
        self.assertEqual(rc, -1)


if __name__ == '__main__':
    unittest.main()
