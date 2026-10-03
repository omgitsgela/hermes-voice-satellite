#!/usr/bin/env python3
"""Host tests of the MCU's actual bounded Wyoming header validator."""
import ctypes as C
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'firmware/esp32/components/wyoming_json'


class Frame(C.Structure):
    _fields_ = [('type', C.c_char * 97), ('data_length', C.c_size_t),
                ('payload_length', C.c_size_t)]


class JsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        libpath = Path(cls.tmp.name) / 'json.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-shared', '-fPIC', '-DCJSON_NESTING_LIMIT=16',
                        '-I' + str(CORE / 'vendor/cjson'),
                        '-I' + str(CORE.parent / 'wyoming_wire'),
                        str(CORE / 'wyoming_json.c'),
                        str(CORE / 'vendor/cjson/cJSON.c'), '-lm',
                        '-o', str(libpath)], check=True)
        cls.lib = C.CDLL(str(libpath))
        cls.lib.wy_parse_header.argtypes = [C.c_char_p, C.c_size_t, C.POINTER(Frame)]

    def parse(self, header):
        frame = Frame()
        result = self.lib.wy_parse_header(header, len(header), C.byref(frame))
        return result, frame

    def test_python_compatible_header_lengths(self):
        header = b'{"type":"audio-chunk","version":"1.0.0","data_length":42,"payload_length":640}'
        result, frame = self.parse(header)
        self.assertEqual(result, 0)
        self.assertEqual(frame.type, b'audio-chunk')
        self.assertEqual(frame.data_length, 42)
        self.assertEqual(frame.payload_length, 640)
        result, frame = self.parse(b'{"type":"describe"}')
        self.assertEqual((result, frame.data_length, frame.payload_length), (0, 0, 0))

    def test_untrusted_lengths_rejected_before_integer_conversion(self):
        for key, bound in [('data_length', 4096), ('payload_length', 16384)]:
            for value in [-1, 0.5, True, False, None, '42', {}, [], bound + 1, 1e300]:
                with self.subTest(key=key, value=value):
                    header = json.dumps({'type': 'audio-chunk', key: value}).encode()
                    self.assertNotEqual(self.parse(header)[0], 0)
            header = json.dumps({'type': 'audio-chunk', key: bound}).encode()
            self.assertEqual(self.parse(header)[0], 0)
    def test_malformed_ambiguous_and_nonobject_headers_rejected(self):
        invalid = [b'[]', b'null', b'{}', b'{"type":42}', b'{"type":""}',
                   b'{"type":"bad type"}', b'{"type":"info"}garbage',
                   b'{"type":"info"}\x00garbage', b'{"type":"info","data":[]}',
                   b'{"type":"info","data":null}',
                   b'{"type":"info","type":"describe"}',
                   b'{"type":"info","payload_length":0,"payload_length":5}',
                   b'{"type":"info","payload_length":1e999}',
                   b'{"type":"in\\u0000fo"}',
                   b'{"type":"info","data_length":01}',
                   b'{"type":"info","data_length":1.}',
                   b'{"type":"info","data_length":1e}',
                   b'{"type":"' + b'a' * 97 + b'"}']
        for header in invalid:
            with self.subTest(header=header):
                self.assertNotEqual(self.parse(header)[0], 0)
        self.assertEqual(self.parse(b' {"type":"info","data":{}}  ')[0], 0)
        self.assertNotEqual(self.parse(b' ' * 1024)[0], 0)


if __name__ == '__main__':
    unittest.main()
