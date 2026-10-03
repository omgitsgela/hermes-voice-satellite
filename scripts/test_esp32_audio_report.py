#!/usr/bin/env python3
"""Host tests for the audio-bring-up report core: what a bring-up did, rendered as
self-describing console lines, with ESP-IDF types kept out so it is testable on a host.

The bring-up proves the audio hardware path is initialized and the codec answers, but by
design it captures nothing and plays nothing. The report must say which steps ran and
succeeded, and it must never imply capture or playback happened.
"""
import ctypes as C
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'firmware/esp32/components/audio_report'


class BringupResult(C.Structure):
    _fields_ = [
        ('i2s', C.c_int),             # 0 initialized, -1 init failed
        ('i2c', C.c_int),             # 0 codec ok, -1 no codec, -2 bus error
        ('version_present', C.c_int), # 1 when a version was read from the codec
        ('version', C.c_ubyte * 3),   # major, minor, patch
    ]


class AudioReportTests(unittest.TestCase):
    def library(self):
        self.assertTrue((CORE / 'audio_report.c').exists(), 'audio_report core missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'audio.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(CORE / 'audio_report.c'), '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.audio_report_append.argtypes = [C.c_char_p, C.c_size_t, C.c_size_t, C.c_char_p]
        lib.audio_report_append.restype = C.c_size_t
        lib.audio_bringup_render.argtypes = [C.POINTER(BringupResult), C.c_char_p, C.c_size_t]
        lib.audio_bringup_render.restype = C.c_size_t
        return lib

    def append(self, lib, capacity, used, text):
        buf = C.create_string_buffer(capacity)
        n = lib.audio_report_append(buf, capacity, used, text)
        return n, buf.value

    def render(self, lib, i2s, i2c, version_present, version=(1, 2, 3)):
        result = BringupResult(i2s=i2s, i2c=i2c, version_present=version_present,
                               version=(C.c_ubyte * 3)(*version))
        buf = C.create_string_buffer(1024)
        n = lib.audio_bringup_render(C.byref(result), buf, len(buf))
        return n, buf.value

    # --- bounds: the one place they are enforced (same contract as wifi_report_append) ---
    def test_append_never_overruns_and_keeps_terminator(self):
        lib = self.library()
        # Returns the new running offset, clamped so a NUL always fits, exactly like wifi_report.
        n, val = self.append(lib, 5, 0, b'abcde')
        self.assertEqual(n, 4, 'only 4 of 5 fit in a 5-byte buffer (room for the NUL)')
        self.assertEqual(val, b'abcd')
        n, val = self.append(lib, 5, 0, b'abcd')
        self.assertEqual(n, 4)
        self.assertEqual(val, b'abcd')
        # No room left: terminated in place, offset clamped, nothing written.
        n, _ = self.append(lib, 6, 5, b'xyz')
        self.assertEqual(n, 5, 'a full buffer returns its clamped offset and writes no data')

    def test_append_empty_text_stores_nothing(self):
        lib = self.library()
        n, _ = self.append(lib, 16, 0, b'')
        self.assertEqual(n, 0)
        n, val = self.append(lib, 16, 0, b'abc')
        self.assertEqual(n, 3)
        self.assertEqual(val, b'abc')

    # --- the bring-up report lines ---
    def test_report_names_i2s_init_and_i2c_probe(self):
        lib = self.library()
        n, val = self.render(lib, 0, 0, 1)  # i2s ok, codec ok, version read
        text = val.decode()
        self.assertIn('i2s=initialized', text)
        self.assertIn('i2c=codec_ok', text)
        self.assertIn('version=1.2.3', text)
        self.assertTrue(all(part == '' or '=' in part for part in text.splitlines()),
                        f'every non-empty line must be a field=value pair: {text!r}')

    def test_version_is_omitted_when_not_present(self):
        lib = self.library()
        _, val = self.render(lib, 0, 0, 0)
        self.assertIn('i2c=codec_ok', val.decode())
        self.assertNotIn('version=', val.decode(),
                         'a codec that answered but returned no version must not be given one')

    def test_i2s_failure_is_named_not_swallowed(self):
        lib = self.library()
        _, val = self.render(lib, -1, 0, 0)
        self.assertIn('i2s=init_failed', val.decode())

    def test_no_codec_is_named_and_gets_no_version(self):
        lib = self.library()
        _, val = self.render(lib, 0, -1, 0)
        self.assertIn('i2c=no_codec', val.decode())
        self.assertNotIn('version=', val.decode())

    def test_i2c_bus_error_is_named(self):
        lib = self.library()
        _, val = self.render(lib, 0, -2, 0)
        self.assertIn('i2c=bus_error', val.decode())

    def test_report_never_implies_capture_or_playback(self):
        lib = self.library()
        # The point of item 6: bring-up proves the path and the codec, and stops. It must not
        # say the microphone is open or the speaker is armed.
        for (i2s, i2c, ver) in ((0, 0, 1), (0, 0, 0), (0, -1, 0), (-1, 0, 0), (-1, -2, 0)):
            _, val = self.render(lib, i2s, i2c, ver)
            low = val.decode().lower()
            self.assertNotIn('capture', low)
            self.assertNotIn('playback', low)
            self.assertNotIn('armed', low)


if __name__ == '__main__':
    unittest.main()
