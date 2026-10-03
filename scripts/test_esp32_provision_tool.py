#!/usr/bin/env python3
"""Bounded client-side USB provisioning response handling."""
from pathlib import Path
import importlib.util
import unittest

SCRIPT = Path(__file__).with_name('esp32_provision.py')


class ProvisionToolTests(unittest.TestCase):
    def test_fragmented_replies_errors_and_bounded_failures(self):
        self.assertTrue(SCRIPT.exists(), 'USB provisioning client missing')
        spec = importlib.util.spec_from_file_location('provision_tool', SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Stream:
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

        connection = Stream(b'boot log\nPROVISION READY 1\nPROVISION OK UNCONFIGURED\n')
        self.assertEqual(module.exchange(connection, 'STATUS'), 'OK UNCONFIGURED')
        self.assertEqual(connection.sent, b'STATUS\n')
        with self.assertRaisesRegex(RuntimeError, 'ERR STORAGE'):
            module.exchange(Stream(b'PROVISION ERR STORAGE\n'), 'STATUS')
        for command in ['STATUS\nCLEAR_ENDPOINT', 'STATUS\0', 'x' * 256, '\u00e9']:
            stream = Stream(b'PROVISION OK UNCONFIGURED\n')
            with self.assertRaises(ValueError):
                module.exchange(stream, command)
            self.assertEqual(stream.sent, b'')
        with self.assertRaises(TimeoutError):
            module.exchange(Stream(b''), 'STATUS', timeout=0.01)
        with self.assertRaisesRegex(RuntimeError, 'oversize'):
            module.exchange(Stream(b'x' * 1025), 'STATUS')


if __name__ == '__main__':
    unittest.main()
