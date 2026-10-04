#!/usr/bin/env python3
"""Host tests for the run-wifi-connect helper's passphrase handling.

The join script stages the passphrase byte for byte, and a WPA passphrase may legally contain
leading or trailing spaces: stripping them stages a wrong key, and the join then fails with a
misleading reason. The provisioning tool's read_psk pins this same contract; the run script's
must not drift from it (that is exactly how it was before this test: a bare .strip()).
"""
from pathlib import Path
import importlib.util
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('run-wifi-connect.py')


def _module():
    # The module name has dashes; importlib's spec path handles that.
    spec = importlib.util.spec_from_file_location('run_wifi_connect', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StagingConsole:
    """A scripted console for the join script's run(): whatever it holds is delivered one byte
    per read (like the real port, which dribbles), and writes are recorded so the test can
    prove which commands went down the wire and in what order. The script's data is NOT the
    input buffer run() drains: it is the device's output stream."""

    def __init__(self, data: bytes):
        self.data = bytearray(data)
        self.sent = b''

    def reset_input_buffer(self):
        # The real port's pending input; the scripted stream is the device's own output.
        pass

    def write(self, data):
        self.sent += data
        return len(data)

    def flush(self):
        pass

    def read(self, size):
        result = bytes(self.data[:1])
        del self.data[:1]
        return result

    def close(self):
        pass


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.module = _module()

    def test_a_refused_stage_is_not_consumed_as_a_staged_acknowledgment(self):
        # The device rejects the PSK (a storage fault, or a shape the console refuses). Before
        # the fix, run-wifi-connect waited for the bare b'PROVISION ' prefix, so the refusal
        # line was consumed and printed as the "staged" PSK ack, the join was sent anyway, and
        # the credential error came back misdiagnosed as "INCOMPLETE: no join block" (exit 3).
        # The script must recognize the refusal, not send the join, and surface exit 2.
        console = StagingConsole(
            b'PROVISION READY 1 NETWORK_INACTIVE AUDIO_INACTIVE\n'
            b'PROVISION OK WIFI SSID STAGED\n'
            b'PROVISION ERR WIFI STORAGE\n')
        code = self.module.run(console, 'GL-Test-AP', b'SuperSecret123',
                               stage_timeout=0.5, ready_timeout=0.5, join_timeout=1)
        self.assertEqual(code, 2, 'a refused stage is a refusal, not an incomplete join')
        self.assertNotIn(b'WIFI_CONNECT', console.sent,
                         'the join must not be sent when a stage was refused')

    def test_staging_waits_for_the_exact_acknowledgments(self):
        # The stage waits are for the exact ack lines, so a refusal that arrives BETWEEN the two
        # stages cannot be mistaken for the SSID's ack (its text shares the PROVISION prefix).
        console = StagingConsole(
            b'PROVISION READY 1 NETWORK_INACTIVE AUDIO_INACTIVE\n'
            b'PROVISION ERR WIFI NOT_STAGED\n'
            b'PROVISION OK WIFI SSID STAGED\n'
            b'PROVISION OK WIFI PSK STAGED\n'
            b'PROVISION WIFI CONNECT BEGIN\n'
            b'status=connected profile=1\n'
            b'PROVISION WIFI CONNECT END\n')
        code = self.module.run(console, 'GL-Test-AP', b'SuperSecret123',
                               stage_timeout=0.5, ready_timeout=0.5, join_timeout=1)
        self.assertEqual(code, 0, 'both exact acks arrive, so the run completes')
        self.assertIn(b'WIFI_CONNECT', console.sent)

    def test_a_join_that_fails_reports_failure_not_incompleteness(self):
        console = StagingConsole(
            b'PROVISION READY 1 NETWORK_INACTIVE AUDIO_INACTIVE\n'
            b'PROVISION OK WIFI SSID STAGED\n'
            b'PROVISION OK WIFI PSK STAGED\n'
            b'PROVISION WIFI CONNECT BEGIN\n'
            b'status=failed reason=201:NO_AP_FOUND\n'
            b'PROVISION WIFI CONNECT END\n')
        code = self.module.run(console, 'GL-Test-AP', b'SuperSecret123',
                               stage_timeout=0.5, ready_timeout=0.5, join_timeout=1)
        self.assertEqual(code, 2, 'a failed join is a failed join, not an incomplete one')


class RunWifiConnectTests(unittest.TestCase):
    def test_read_psk_strips_only_the_line_terminator(self):
        module = _module()
        with tempfile.TemporaryDirectory() as scratch:
            plain = Path(scratch) / 'plain.psk'
            plain.write_bytes(b'SuperSecret123\n')
            self.assertEqual(module.read_psk(plain), b'SuperSecret123',
                             'the trailing newline is stripped, the value is not')
            crlf = Path(scratch) / 'crlf.psk'
            crlf.write_bytes(b'SuperSecret123\r\n')
            self.assertEqual(module.read_psk(crlf), b'SuperSecret123')
            spaced = Path(scratch) / 'spaced.psk'
            spaced.write_bytes(b' padded key \n')
            self.assertEqual(module.read_psk(spaced), b' padded key ',
                             'a legal passphrase with edge spaces must survive byte for byte')
            rawkey = Path(scratch) / 'raw.psk'
            rawkey.write_bytes(b'0123456789abcdef' * 4)
            self.assertEqual(module.read_psk(rawkey), b'0123456789abcdef' * 4)
            empty = Path(scratch) / 'empty.psk'
            empty.write_bytes(b'')
            with self.assertRaises(SystemExit):
                module.read_psk(empty)
            only_newline = Path(scratch) / 'nl.psk'
            only_newline.write_bytes(b'\n')
            with self.assertRaises(SystemExit):
                module.read_psk(only_newline)
            # A malformed file is refused here, before a byte reaches the device: before the
            # shape check was added it sailed straight past and got misdiagnosed as an
            # incomplete join block.
            short = Path(scratch) / 'short.psk'
            short.write_bytes(b'short\n')
            with self.assertRaises(SystemExit):
                module.read_psk(short)
            unprintable = Path(scratch) / 'binary.psk'
            unprintable.write_bytes(b'Secr\x00t')
            with self.assertRaises(SystemExit):
                module.read_psk(unprintable)
            too_long = Path(scratch) / 'long.psk'
            too_long.write_bytes(b'x' * 64)
            with self.assertRaises(SystemExit):
                module.read_psk(too_long)
            self.assertEqual(module.psk_shape(b'12345678'), 'ascii-passphrase')
            self.assertEqual(module.psk_shape(b'0' * 64), '64-hex-raw-key')
            self.assertEqual(module.psk_shape(b'1234567'), '')
            self.assertEqual(module.psk_shape(b'x' * 64), '')


if __name__ == '__main__':
    unittest.main()
