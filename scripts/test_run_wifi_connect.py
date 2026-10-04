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


if __name__ == '__main__':
    unittest.main()
