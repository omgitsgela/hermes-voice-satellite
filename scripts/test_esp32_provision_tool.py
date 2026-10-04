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
            # Only the line terminator is stripped. A WPA passphrase may legally contain
            # leading or trailing spaces, and stripping them silently would stage a wrong key.
            spaced = Path(scratch) / 'spaced.psk'
            spaced.write_bytes(b' padded key \n')
            self.assertEqual(module.read_psk(spaced), b' padded key ',
                             'a legal passphrase with edge spaces must survive byte for byte')

    def test_block_reader_stops_at_an_end_marker(self):
        module = _module()
        # The marker is matched as a whole line (the scan's AP lines go out raw), so the test
        # uses the real full end marker, the same one main() hands exchange_block.
        module_terminator = module.block_terminator('scan')
        block = (b'PROVISION WIFI SCAN BEGIN\n'
                 b'GL-Test-AP ch=6 rssi=-42 auth=WPA2\n'
                 b'PROVISION WIFI SCAN END\n')
        connection = Stream(block)
        lines = module.exchange_block(connection, 'WIFI_SCAN', terminator=module_terminator,
                                      timeout=0.2)
        self.assertEqual(connection.sent, b'WIFI_SCAN\n')
        self.assertIn('PROVISION WIFI SCAN BEGIN', lines)
        self.assertIn('GL-Test-AP ch=6 rssi=-42 auth=WPA2', lines)
        self.assertIn('PROVISION WIFI SCAN END', lines)

    def test_scan_block_survives_a_network_name_that_contains_end(self):
        module = _module()
        # A scan whose AP names contain the marker's text. The device sends AP lines raw
        # (no PROVISION prefix), so a bare 'END' substring terminator would stop the block at
        # WENDYS and lose the rest plus the real end marker. The framed block must be closed by
        # its whole end-marker line only.
        self.assertEqual(module.block_terminator('scan'), 'PROVISION WIFI SCAN END')
        self.assertEqual(module.block_terminator('connect'), 'PROVISION WIFI CONNECT END')
        self.assertIsNone(module.block_terminator('list-profiles'))
        block = (b'PROVISION WIFI SCAN BEGIN\n'
                 b'WENDYS ch=6 rssi=-42 auth=WPA2\n'
                 b'LEGEND ch=11 rssi=-60 auth=WPA2\n'
                 b'PROVISION WIFI SCAN END\n')
        connection = Stream(block)
        lines = module.exchange_block(connection, 'WIFI_SCAN',
                                      terminator=module.block_terminator('scan'), timeout=0.2)
        for expected in ('WENDYS ch=6 rssi=-42 auth=WPA2',
                         'LEGEND ch=11 rssi=-60 auth=WPA2',
                         'PROVISION WIFI SCAN END'):
            self.assertIn(expected, lines, 'the block must reach its real end marker')

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

    def test_a_framed_block_with_a_silent_gap_midway_reaches_its_end_marker(self):
        module = _module()
        # The device's WIFI_CONNECT block has a real silent gap in it: BEGIN and the profile
        # line come out, then wifi_station_connect blocks for up to 20 s, then the outcome line
        # and the END marker arrive together. A reader that treats "two quiet reads after a
        # line" as the end of a framed block returns mid-join with no outcome and no END marker,
        # and the tool exits 0 as if the join had been reported. The idle escape is the
        # unframed listing's only end-of-block signal (the listing sends its burst and then
        # stops); a framed block promises its terminator, so silence mid-block must never end
        # it. A stream that is quiet after BEGIN and never sends the marker must therefore time
        # out, not return a partial block.
        with self.assertRaises(TimeoutError):
            module.exchange_block(Stream(b'PROVISION WIFI CONNECT BEGIN\n'
                                         b'profile source=saved index=0 ssid=Alpha\n'),
                                  'WIFI_CONNECT',
                                  terminator=module.block_terminator('connect'),
                                  timeout=0.3)
        # ...and the full device sequence, with the outcome after the silent join, returns
        # complete with the marker.
        block = (b'PROVISION WIFI CONNECT BEGIN\n'
                 b'profile source=saved index=0 ssid=Alpha\n'
                 b'status=connected profile index=0\n'
                 b'PROVISION WIFI CONNECT END\n')
        lines = module.exchange_block(Stream(block), 'WIFI_CONNECT',
                                      terminator=module.block_terminator('connect'), timeout=0.3)
        self.assertIn('status=connected profile index=0', lines,
                      'the join outcome must not be lost to a mid-block silence')
        self.assertIn('PROVISION WIFI CONNECT END', lines)


    def test_save_profile_stages_both_and_surfaces_a_failed_save(self):
        module = _module()
        # Happy path: SSID and PSK are each staged (each with its own ack consumed), then the
        # pair is saved. The third line must be the SAVE's acknowledgment, not the PSK's: before
        # the fix the PSK was written raw with no ack read, so the save's exchange() consumed the
        # PSK's stale ack and reported the save as whatever the PSK answered with.
        stream = Stream(b'PROVISION OK WIFI SSID STAGED\n'
                        b'PROVISION OK WIFI PSK STAGED\n'
                        b'PROVISION OK WIFI PROFILE SAVED n=1\n')
        with tempfile.TemporaryDirectory() as scratch:
            psk_file = Path(scratch) / 'psk'
            psk_file.write_bytes(b'SuperSecret123\n')
            lines = module.save_profile(stream, 'HomeNet', psk_file)
        self.assertEqual(lines, ['OK WIFI SSID STAGED', 'OK WIFI PSK STAGED',
                                 'OK WIFI PROFILE SAVED n=1'])
        # The wire shows all three commands in order; the PSK line is the full
        # 13 + len(psk) + 1 bytes (the old short count of 12 + len(psk) made this a false
        # "partial write" for every passphrase).
        self.assertEqual(
            stream.sent,
            b'SET_WIFI_SSID HomeNet\n'
            b'SET_WIFI_PSK SuperSecret123\n'
            b'WIFI_PROFILE_SAVE\n')
        # A save that fails after both stages must surface the SAVE's error. Before the fix the
        # PSK's ack was read as the save's result, so a failed save reported success and the real
        # ERR line was left stranded in the buffer.
        stream2 = Stream(b'PROVISION OK WIFI SSID STAGED\n'
                         b'PROVISION OK WIFI PSK STAGED\n'
                         b'PROVISION ERR WIFI PROFILE FULL\n')
        with tempfile.TemporaryDirectory() as scratch:
            psk_file = Path(scratch) / 'psk'
            psk_file.write_bytes(b'SuperSecret123\n')
            with self.assertRaisesRegex(RuntimeError, 'ERR WIFI PROFILE FULL'):
                module.save_profile(stream2, 'HomeNet', psk_file)


if __name__ == '__main__':
    unittest.main()
