#!/usr/bin/env python3
"""Bounded client-side USB provisioning response handling, including the multi-network protocol."""
from pathlib import Path
import importlib.util
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('esp32_provision.py')


def _module():
    spec = importlib.util.spec_from_file_location('provision_tool', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Stream:
    """A byte-by-byte serial stand-in: one byte per read, and empty bytes once it runs dry. The
    empty reads are what let a block reader see the console go quiet, the same way a real idle
    serial port behaves."""

    def __init__(self, data):
        self.data = bytearray(data)
        self.sent = b''

    def write(self, data):
        self.sent += data
        return len(data)

    def read(self, length):
        result = bytes(self.data[:1])
        del self.data[:1]
        return result


class ProvisionToolTests(unittest.TestCase):
    def test_fragmented_replies_errors_and_bounded_failures(self):
        module = _module()
        self.assertTrue(SCRIPT.exists(), 'USB provisioning client missing')
        connection = Stream(b'boot log\nPROVISION READY 1\nPROVISION OK UNCONFIGURED\n')
        self.assertEqual(module.exchange(connection, 'STATUS'), 'OK UNCONFIGURED')
        self.assertEqual(connection.sent, b'STATUS\n')
        with self.assertRaisesRegex(RuntimeError, 'ERR STORAGE'):
            module.exchange(Stream(b'PROVISION ERR STORAGE\n'), 'STATUS')
        for command in ['STATUS\nCLEAR_ENDPOINT', 'STATUS\x00', 'x' * 256, '\u00e9']:
            stream = Stream(b'PROVISION OK UNCONFIGURED\n')
            with self.assertRaises(ValueError):
                module.exchange(stream, command)
            self.assertEqual(stream.sent, b'')
        with self.assertRaises(TimeoutError):
            module.exchange(Stream(b''), 'STATUS', timeout=0.01)
        with self.assertRaisesRegex(RuntimeError, 'oversize'):
            module.exchange(Stream(b'x' * 1025), 'STATUS')

    def test_psk_shape_accepts_both_wpa2_forms_and_rejects_the_rest(self):
        module = _module()
        self.assertEqual(module.psk_shape(b'12345678'), 'ascii-passphrase')
        self.assertEqual(module.psk_shape(b'p' * 63), 'ascii-passphrase')
        self.assertEqual(module.psk_shape(b'0123456789abcdef' * 4), '64-hex-raw-key')
        self.assertEqual(module.psk_shape(b'a' * 64), '64-hex-raw-key',
                         'a is a hex digit, so 64 of them is a valid raw key (matches the core test)')
        # A space is printable (0x20), so a passphrase may contain one: only control bytes and
        # out-of-range lengths are refused (same rule the core validator pins).
        self.assertEqual(module.psk_shape(b'has space'), 'ascii-passphrase')
        for bad in (b'', b'1234567', b'p' * 64, b'x' * 64, b'z' * 64, b'12345678\x01x',
                    b'P' * 64, b'1234567\x7f', b'12345678\n'):
            self.assertEqual(module.psk_shape(bad), '', f'{bad!r} must not be a valid PSK')
        # 8 bytes of hex is an ASCII passphrase, not a raw key: the raw-key form is exactly 64.
        self.assertEqual(module.psk_shape(b'01234567'), 'ascii-passphrase')


    def test_read_psk_reads_validates_and_refuses(self):
        module = _module()
        with tempfile.TemporaryDirectory() as scratch:
            good = Path(scratch) / 'good.psk'
            good.write_bytes(b'SuperSecret123\n')
            self.assertEqual(module.read_psk(good), b'SuperSecret123',
                             'the trailing newline is stripped, the value is not')
            rawkey = Path(scratch) / 'raw.psk'
            rawkey.write_bytes(b'0123456789abcdef' * 4)
            self.assertEqual(module.read_psk(rawkey), b'0123456789abcdef' * 4)
            empty = Path(scratch) / 'empty.psk'
            empty.write_bytes(b'')
            with self.assertRaises(SystemExit):
                module.read_psk(empty)
            short = Path(scratch) / 'short.psk'
            short.write_bytes(b'1234567')
            with self.assertRaises(SystemExit):
                module.read_psk(short)

    def test_block_reader_stops_at_an_end_marker(self):
        module = _module()
        block = (b'PROVISION WIFI SCAN BEGIN\n'
                 b'GL-Test-AP ch=6 rssi=-42 auth=WPA2\n'
                 b'PROVISION WIFI SCAN END\n')
        connection = Stream(block)
        lines = module.exchange_block(connection, 'WIFI_SCAN', terminator='END', timeout=0.2)
        self.assertEqual(connection.sent, b'WIFI_SCAN\n')
        self.assertIn('PROVISION WIFI SCAN BEGIN', lines)
        self.assertIn('GL-Test-AP ch=6 rssi=-42 auth=WPA2', lines)
        self.assertIn('PROVISION WIFI SCAN END', lines)

    def test_block_reader_collects_a_listing_that_has_no_end_marker(self):
        module = _module()
        # Two profiles, then the console goes quiet (the stream runs dry): the reader must stop on
        # the idle, not wait for a marker the listing never sends.
        block = (b'PROVISION WIFI PROFILE index=0 ssid=Alpha psk=present\n'
                 b'PROVISION WIFI PROFILE index=1 ssid=Gamma psk=present\n')
        connection = Stream(block)
        lines = module.exchange_block(connection, 'WIFI_PROFILES', terminator=None, timeout=0.2)
        self.assertEqual(connection.sent, b'WIFI_PROFILES\n')
        self.assertIn('index=0 ssid=Alpha psk=present', lines)
        self.assertIn('index=1 ssid=Gamma psk=present', lines)
        self.assertNotIn('END', lines, 'a listing has no END marker and must not be made to wait for one')

    def test_block_reader_is_bounded_and_validates_the_command(self):
        module = _module()
        with self.assertRaisesRegex(RuntimeError, 'oversize'):
            module.exchange_block(Stream(b'x' * 4097), 'WIFI_SCAN', terminator='END')
        with self.assertRaises(ValueError):
            module.exchange_block(Stream(b''), 'W' * 256, terminator='END')


if __name__ == '__main__':
    unittest.main()
