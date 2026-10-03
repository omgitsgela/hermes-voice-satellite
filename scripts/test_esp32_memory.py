#!/usr/bin/env python3
"""Test the exact memory-pattern helper used by the on-device diagnostic."""
import ctypes as C
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'firmware/esp32/components/memory_probe'


class MemoryTests(unittest.TestCase):
    def test_detects_corrupted_pattern(self):
        source = CORE / 'memory_probe.c'
        self.assertTrue(source.exists(), 'memory probe missing')
        with tempfile.TemporaryDirectory() as tmp:
            libpath = Path(tmp) / 'probe.so'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-shared', '-fPIC', str(source), '-o', str(libpath)], check=True)
            lib = C.CDLL(str(libpath))
            lib.memory_probe_fill.argtypes = [C.c_void_p, C.c_size_t, C.c_uint8]
            lib.memory_probe_check.argtypes = [C.c_void_p, C.c_size_t, C.c_uint8]
            for length in [1, 255, 256, 257, 1048576]:
                buffer = (C.c_uint8 * length)()
                for seed in [0x5a, 0xa5]:
                    lib.memory_probe_fill(buffer, length, seed)
                    self.assertEqual(lib.memory_probe_check(buffer, length, seed), 0)
                    for index in [0, length // 2, length - 1]:
                        buffer[index] ^= 1
                        self.assertNotEqual(lib.memory_probe_check(buffer, length, seed), 0)
                        buffer[index] ^= 1


if __name__ == '__main__':
    unittest.main()
