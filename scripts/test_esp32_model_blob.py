#!/usr/bin/env python3
"""The wake model blob's pure layout logic (wy_model.c), host-tested.

The blob is the file format of the `model` data partition (D3): one header
(magic, version, model name, data length, SHA-256) followed by the model
bytes. This module owns only the hash-free layout logic -- the provisioner
computes the SHA-256 (in Python), the device glue re-checks it (mbedTLS) -- so
the exact production source compiles on the host with -Werror and is tested
directly. A blob that fails any check must be rejected whole: the detector
falls back to its no-model state, it never runs a garbage scorer.
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
VERSION = 1
NAME_MAX = 64
SHA_LEN = 32
HEADER_SIZE = 4 + 2 + 2 + NAME_MAX + 4 + SHA_LEN   # 108


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
        lib.wy_model_header_pack.argtypes = [C.c_char_p, C.c_uint32,
                                             C.POINTER(C.c_uint8),
                                             C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
        lib.wy_model_header_pack.restype = C.c_int
        lib.wy_model_header_parse.argtypes = [C.c_void_p, C.c_size_t, C.c_void_p]
        lib.wy_model_header_parse.restype = C.c_int
        return lib

    def header_struct(self, lib):
        class H(C.Structure):
            _fields_ = [('magic', C.c_uint32), ('version', C.c_uint16),
                        ('reserved', C.c_uint16), ('name', C.c_char * NAME_MAX),
                        ('data_len', C.c_uint32), ('sha256', C.c_uint8 * SHA_LEN)]
        self._keepalive.append(H)
        return H

    def pack(self, lib, name, data_len, sha):
        out = C.create_string_buffer(HEADER_SIZE)
        out_len = C.c_size_t(0)
        rc = lib.wy_model_header_pack(name, data_len, (C.c_uint8 * SHA_LEN)(*sha),
                                      out, HEADER_SIZE, C.byref(out_len))
        return rc, bytes(out.raw[:HEADER_SIZE]), out_len.value

    def parse(self, lib, blob):
        H = self.header_struct(lib)
        h = H()
        buf = C.create_string_buffer(len(blob))
        C.memmove(buf, blob, len(blob))
        rc = lib.wy_model_header_parse(buf, len(blob), C.byref(h))
        return rc, h

    def good_blob(self, name=b'hey_hermes', data_len=2048, sha=None,
                  model_bytes=None):
        """The canonical layout this module must round-trip: the exact bytes a
        provisioner emits (header + the model bytes it describes) and the
        device reads back. `model_bytes` defaults to data_len filler bytes."""
        if sha is None:
            sha = bytes(range(SHA_LEN))
        name_b = name[:NAME_MAX - 1].ljust(NAME_MAX, b'\0')
        header = (struct.pack('<IHH', MAGIC, VERSION, 0) + name_b
                  + struct.pack('<I', data_len) + sha)
        if model_bytes is None:
            model_bytes = bytes([(i * 7) & 0xFF for i in range(data_len)])
        return header + model_bytes

    # -- pack -------------------------------------------------------------------
    def test_pack_emits_the_canonical_layout(self):
        lib = self.library()
        rc, raw, out_len = self.pack(lib, b'hey_hermes', 2048, bytes(range(SHA_LEN)))
        self.assertEqual(rc, 0)
        self.assertEqual(out_len, HEADER_SIZE)
        # Pack writes the HEADER only; the model bytes follow it on the wire.
        self.assertEqual(raw, self.good_blob()[:HEADER_SIZE])

    def test_pack_rejects_short_buffers_and_nulls(self):
        lib = self.library()
        rc, _, _ = self.pack(lib, b'hey_hermes', 2048, bytes(SHA_LEN))
        self.assertEqual(rc, 0)
        # A buffer shorter than the header is a bad argument, not a truncation.
        short = C.create_string_buffer(HEADER_SIZE - 1)
        out_len = C.c_size_t(0)
        self.assertEqual(
            lib.wy_model_header_pack(b'hey_hermes', 2048,
                                     (C.c_uint8 * SHA_LEN)(*bytes(SHA_LEN)),
                                     short, HEADER_SIZE - 1, C.byref(out_len)), -1)
        self.assertEqual(lib.wy_model_header_pack(None, 2048,
                                                  (C.c_uint8 * SHA_LEN)(*bytes(SHA_LEN)),
                                                  C.create_string_buffer(HEADER_SIZE),
                                                  HEADER_SIZE, C.byref(out_len)), -1)
        self.assertEqual(lib.wy_model_header_pack(b'hey_hermes', 2048, None,
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

    # -- parse ------------------------------------------------------------------
    def test_parse_round_trips_the_canonical_blob(self):
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

    def test_parse_rejects_bad_magic(self):
        lib = self.library()
        blob = bytearray(self.good_blob())
        blob[0:1] = b'X'
        rc, _ = self.parse(lib, bytes(blob))
        self.assertEqual(rc, -1)

    def test_parse_rejects_wrong_version(self):
        lib = self.library()
        blob = bytearray(self.good_blob())
        struct.pack_into('<H', blob, 4, 2)
        rc, _ = self.parse(lib, bytes(blob))
        self.assertEqual(rc, -1)

    def test_parse_rejects_a_truncated_header(self):
        lib = self.library()
        blob = self.good_blob()
        for cut in (0, 4, 8, HEADER_SIZE - 1):
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
        reject it. A length that EXACTLY fills the remainder is valid."""
        lib = self.library()
        blob = self.good_blob(data_len=16)   # a blob whose length fits exactly
        rc, h = self.parse(lib, blob)
        self.assertEqual(rc, 0)
        self.assertEqual(h.data_len, 16)
        # Same bytes, but the header's data_len field now claims one byte more
        # than the blob holds: corrupt, reject.
        patched = bytearray(blob)
        struct.pack_into('<I', patched, 72, 17)
        rc, _ = self.parse(lib, bytes(patched))
        self.assertEqual(rc, -1)

    def test_parse_rejects_blank_flash(self):
        """An erased (never-provisioned) region reads 0xFF: a valid region is
        one with a real header, so all-0xFF is a no-model state, not a blob."""
        lib = self.library()
        rc, _ = self.parse(lib, b'\xff' * HEADER_SIZE)
        self.assertEqual(rc, -1)


if __name__ == '__main__':
    unittest.main()
