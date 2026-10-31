#!/usr/bin/env python3
"""Host tests for the provisioned TLS credential blob and the pre-dial TLS policy.

The blob format has one producer (scripts/tls_credential_blob.py, which the provisioning script
uses) and one consumer (firmware/esp32/main/tls_credentials.c). These tests compile the exact
production C source on the host and feed it blobs built by the real producer, so the two cannot
drift apart silently. The malformed cases are built by hand here, because a corrupted flash region
does not use the producer.
"""
import ctypes as C
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import tls_credential_blob as blob_format  # noqa: E402  (path set up above)

SOURCE = ROOT / 'firmware/esp32/main/tls_credentials.c'
HEADER_LEN = blob_format.HEADER_LEN
PIN_LEN = blob_format.SERVER_PIN_LEN

CA_PEM = (b'-----BEGIN CERTIFICATE-----\nMIIFakeCA\n-----END CERTIFICATE-----\n\x00')
CERT_PEM = (b'-----BEGIN CERTIFICATE-----\nMIIFakeDevice\n-----END CERTIFICATE-----\n\x00')
KEY_PEM = (b'-----BEGIN PRIVATE KEY-----\nMIIFakeKey\n-----END PRIVATE KEY-----\n\x00')


class Credentials(C.Structure):
    """Mirrors tls_credentials in tls_credentials.h, field for field and in order. The C code fills
    it field by field rather than copying a packed header, so host padding cannot change meaning."""
    _fields_ = [('ca', C.c_char_p), ('ca_len', C.c_uint32),
                ('cert', C.c_char_p), ('cert_len', C.c_uint32),
                ('key', C.c_char_p), ('key_len', C.c_uint32),
                ('server_pin', C.c_ubyte * PIN_LEN),
                ('server_host', C.c_char * blob_format.SERVER_HOST_MAX),
                ('server_port', C.c_uint32),
                ('total_len', C.c_uint32)]


def good_blob(**overrides):
    pin = overrides.pop('pin', bytes(range(PIN_LEN)))
    host = overrides.pop('host', 'gateway.example')
    port = overrides.pop('port', 10702)
    return blob_format.build_blob(CA_PEM, CERT_PEM, KEY_PEM, pin, host, port, **overrides)


def raw_blob(ca_pem=CA_PEM, cert_pem=CERT_PEM, key_pem=KEY_PEM, pin=None, host='gateway.example',
             port=10702, version=blob_format.VERSION, magic=blob_format.MAGIC,
             header_len=HEADER_LEN, crc=None, total_len=None, pad=b''):
    """Build a blob without the producer's sanity checks, the way a corrupted or hand-edited flash
    region would look. Only the tests use this: the device must refuse what it produces."""
    pin = bytes(range(PIN_LEN)) if pin is None else pin
    payload = ca_pem + cert_pem + key_pem
    return blob_format._HEADER.pack(
        magic, version, header_len,
        HEADER_LEN + len(payload) if total_len is None else total_len,
        len(ca_pem), len(cert_pem), len(key_pem), port, pin,
        host.encode('ascii').ljust(blob_format.SERVER_HOST_MAX, b'\x00'),
        zlib.crc32(payload) & 0xFFFFFFFF if crc is None else crc) + payload + pad


class BlobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.library_path = Path(cls.tmp.name) / 'tls_credentials.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(SOURCE), '-o', str(cls.library_path)], check=True)
        cls.lib = C.CDLL(str(cls.library_path))
        cls.lib.tls_credentials_parse.argtypes = [C.c_char_p, C.c_size_t, C.POINTER(Credentials)]
        cls.lib.tls_credentials_parse.restype = C.c_int
        cls.lib.tls_crc32.argtypes = [C.c_char_p, C.c_size_t]
        cls.lib.tls_crc32.restype = C.c_uint32
        cls.lib.tls_clock_is_sane.argtypes = [C.c_int64]
        cls.lib.tls_clock_is_sane.restype = C.c_int
        cls.lib.tls_server_pin_matches.argtypes = [C.c_char_p, C.c_char_p]
        cls.lib.tls_server_pin_matches.restype = C.c_int

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def parse(self, blob):
        out = Credentials()
        result = self.lib.tls_credentials_parse(blob, len(blob), C.byref(out))
        return result, out

    def assert_accepted(self, blob):
        result, out = self.parse(blob)
        self.assertEqual(result, 0, 'a valid blob must be accepted')
        return out

    def assert_rejected(self, blob, why):
        result, _ = self.parse(blob)
        self.assertEqual(result, -1, f'must be rejected: {why}')

    def test_the_real_producer_builds_a_blob_the_device_accepts(self):
        out = self.assert_accepted(good_blob())
        self.assertEqual(out.ca, CA_PEM[:-1])
        self.assertEqual(out.cert, CERT_PEM[:-1])
        self.assertEqual(out.key, KEY_PEM[:-1])
        self.assertEqual(out.ca_len, len(CA_PEM))
        self.assertEqual(out.cert_len, len(CERT_PEM))
        self.assertEqual(out.key_len, len(KEY_PEM))
        self.assertEqual(bytes(out.server_pin), bytes(range(PIN_LEN)))
        self.assertEqual(out.server_host, b'gateway.example')
        self.assertEqual(out.server_port, 10702)
        self.assertEqual(out.total_len, HEADER_LEN + len(CA_PEM + CERT_PEM + KEY_PEM))

    def test_zero_padding_after_the_blob_is_ignored(self):
        self.assert_accepted(good_blob(pad=b'\x00' * 512))

    def test_a_truncated_blob_is_refused(self):
        self.assert_rejected(good_blob()[:HEADER_LEN - 1], 'shorter than the header')

    def test_a_truncated_payload_is_refused(self):
        # The CRC catches this too, but a parser that trusted the declared length would read past
        # the caller's buffer before it ever got to the CRC. The bounds check exists so that the
        # read never happens; this case pins the outcome, and the bound itself is why the read is
        # safe even when the outcome would have been the same.
        self.assert_rejected(good_blob()[:-4], 'payload cut short of the declared total')

    def test_a_single_byte_pem_field_is_refused(self):
        # Too short to hold even a BEGIN line, so it cannot be a PEM block with its terminator.
        self.assert_rejected(raw_blob(ca_pem=b'\x00'), 'a field with no room for a terminator')

    def test_bad_magic_is_refused(self):
        self.assert_rejected(good_blob(magic=b'HVTLS99\x00'), 'not our blob')

    def test_an_unknown_version_is_refused(self):
        self.assert_rejected(good_blob(version=2), 'version from a later format')

    def test_a_different_header_length_is_refused(self):
        self.assert_rejected(good_blob(header_len=HEADER_LEN + 4), 'layout drift')

    def test_a_crc_mismatch_is_refused(self):
        self.assert_rejected(good_blob(crc=0xDEADBEEF), 'corrupted flash')

    def test_a_total_length_that_does_not_add_up_is_refused(self):
        self.assert_rejected(good_blob(total_len=HEADER_LEN + 3), 'block lengths disagree')

    def test_a_gap_after_the_material_is_refused(self):
        """Every byte of the payload must belong to one of the three blocks. A blob that leaves room
        for something the device does not know about is not this format, even when its CRC is
        consistent, so the total is required to be exactly the header plus the three lengths."""
        blocks = CA_PEM + CERT_PEM + KEY_PEM
        gap = b'\x00' * 16
        self.assert_rejected(
            raw_blob(total_len=HEADER_LEN + len(blocks) + len(gap),
                     crc=zlib.crc32(blocks + gap) & 0xFFFFFFFF, pad=gap),
            'a payload longer than the blocks that explain it')

    def test_bytes_past_the_length_the_caller_passed_are_not_material(self):
        """The length the caller passes is authoritative. A parser that trusted the declared total
        would read past the buffer it was given; here the bytes that follow happen to complete the
        blob, so the only thing that can refuse it is the bound itself."""
        full = good_blob()
        out = Credentials()
        self.assertEqual(
            self.lib.tls_credentials_parse(full, len(full) - 4, C.byref(out)), -1,
            'a blob longer than the buffer handed over must be refused, not read past')

    def test_a_total_length_beyond_the_buffer_is_refused(self):
        blob = bytearray(good_blob())
        blob[12:16] = (0xFFFFFFF0).to_bytes(4, 'little')
        self.assert_rejected(bytes(blob), 'an impossible length must not walk off the buffer')

    def test_pem_without_its_terminator_is_refused(self):
        self.assert_rejected(raw_blob(cert_pem=CERT_PEM[:-1] + b'\xff'),
                             'a PEM block mbedTLS would read past')

    def test_pem_with_an_embedded_nul_is_refused(self):
        self.assert_rejected(raw_blob(key_pem=KEY_PEM[:12] + b'\x00' + KEY_PEM[12:]),
                             'a shortened string hiding behind its length')

    def test_a_payload_that_is_not_pem_is_refused(self):
        self.assert_rejected(raw_blob(ca_pem=b'not a pem block\x00'),
                             'the wrong file provisioned')

    def test_an_impossible_port_is_refused(self):
        self.assert_rejected(good_blob(port=0), 'port 0')
        self.assert_rejected(good_blob(port=70000), 'a port that cannot exist')

    def test_material_too_large_for_the_reserved_region_is_refused(self):
        oversized = b'-----BEGIN CERTIFICATE-----\n' + b'A' * 9000 + b'\n-----END CERTIFICATE-----\n\x00'
        self.assert_rejected(raw_blob(ca_pem=oversized),
                             'a blob that would not fit the region the device reads')

    def test_an_unprintable_server_host_is_refused(self):
        self.assert_rejected(raw_blob(host='odd\x01host'),
                             'a corrupted label must not reach the report line')

    def test_parse_blob_enforces_the_device_port_rule(self):
        """parse_blob claims to be the device's own validator, and the provisioning script's
        readback check relies on it: a blob the device would refuse at load must be refused
        before it is written, not read back clean and reported PROVISIONED for a link that can
        never come up. The C side refuses port 0 and anything above 65535, so the Python side
        must refuse them too."""
        with self.assertRaises(ValueError):
            blob_format.parse_blob(good_blob(port=0))
        with self.assertRaises(ValueError):
            blob_format.parse_blob(good_blob(port=70000))

    def test_parse_blob_enforces_the_device_host_rule(self):
        """The device refuses an empty host and a host with non-printable bytes (the label for
        its report line); parse_blob must too, so neither reaches flash via the readback check."""
        with self.assertRaises(ValueError):
            blob_format.parse_blob(raw_blob(host=''))
        with self.assertRaises(ValueError):
            blob_format.parse_blob(raw_blob(host='odd\x01host'))
        with self.assertRaises(ValueError):
            blob_format.parse_blob(raw_blob(host='x' * blob_format.SERVER_HOST_MAX))

    def test_an_empty_server_pin_is_caught_by_the_pin_check(self):
        out = self.assert_accepted(good_blob(pin=b'\x00' * PIN_LEN))
        self.assertEqual(self.lib.tls_server_pin_matches(bytes(range(PIN_LEN)),
                                                         bytes(out.server_pin)), 0,
                         'an all-zero pin must never match a real digest')

    def test_crc32_agrees_with_an_independent_implementation(self):
        for sample in (b'', b'123456789', CA_PEM, CA_PEM + CERT_PEM + KEY_PEM):
            self.assertEqual(self.lib.tls_crc32(sample, len(sample)),
                             zlib.crc32(sample) & 0xFFFFFFFF, f'CRC of {sample[:12]!r}')

    def test_the_clock_gate_refuses_an_unsynced_clock(self):
        self.assertEqual(self.lib.tls_clock_is_sane(0), 0, 'the epoch is not a clock')
        self.assertEqual(self.lib.tls_clock_is_sane(1700000000 - 1), 0, 'just below the floor')
        self.assertEqual(self.lib.tls_clock_is_sane(1700000000), 1, 'the floor itself is usable')
        self.assertEqual(self.lib.tls_clock_is_sane(1780000000), 1, 'a synced 2026 clock')

    def test_the_server_pin_comparison_compares_every_byte(self):
        reference = bytes(range(PIN_LEN))
        self.assertEqual(self.lib.tls_server_pin_matches(reference, reference), 1)
        for index in range(PIN_LEN):
            changed = bytearray(reference)
            changed[index] ^= 0x01
            self.assertEqual(
                self.lib.tls_server_pin_matches(bytes(changed), reference), 0,
                f'a difference in byte {index} must be caught')


if __name__ == '__main__':
    unittest.main(verbosity=2)
