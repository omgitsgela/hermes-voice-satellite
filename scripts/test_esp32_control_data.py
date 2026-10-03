#!/usr/bin/env python3
"""Control JSON decoding: canonical body override, bounds and strict input."""
import ctypes as C
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / 'firmware/esp32/components'


class ControlDataTests(unittest.TestCase):
    def test_metadata_merge_rejects_ambiguous_or_malformed_inputs(self):
        parser = COMP / 'wyoming_json'
        self.assertIn('wy_decode_control', (parser / 'wyoming_json.h').read_text(),
                      'control metadata decoder missing')
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / 'control.so'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared',
                            '-fPIC', '-DCJSON_NESTING_LIMIT=16', '-I' + str(parser),
                            '-I' + str(COMP / 'wyoming_wire'), '-I' + str(parser / 'vendor/cjson'),
                            str(parser / 'wyoming_json.c'), str(parser / 'vendor/cjson/cJSON.c'),
                            '-lm', '-o', str(library)], check=True)
            lib = C.CDLL(str(library))
            lib.wy_decode_control.argtypes = [C.c_char_p, C.c_size_t, C.c_char_p,
                                             C.c_size_t, C.POINTER(C.c_void_p)]
            lib.cJSON_PrintUnformatted.argtypes = [C.c_void_p]
            lib.cJSON_PrintUnformatted.restype = C.c_void_p
            lib.cJSON_Delete.argtypes = [C.c_void_p]
            lib.cJSON_free.argtypes = [C.c_void_p]

            def decode(header, body=b''):
                result = C.c_void_p(123)
                code = lib.wy_decode_control(header, len(header), body, len(body), C.byref(result))
                if code:
                    self.assertFalse(result.value)
                    return code, None
                rendered = lib.cJSON_PrintUnformatted(result)
                try:
                    text = C.string_at(rendered)
                finally:
                    lib.cJSON_free(rendered)
                    lib.cJSON_Delete(result)
                return code, text

            self.assertEqual(decode(b'{"type":"describe"}'), (0, b'{}'))
            self.assertEqual(decode(b'{"type":"x","data":{"v":1},"data_length":7}', b'{"v":2}'),
                             (0, b'{"v":2}'))
            self.assertEqual(decode(b'{"type":"x","data":{"v":"caf\xc3\xa9"}}')[0], 0)
            for value in [b'\xc0\xaf', b'\xe0\x80\xaf', b'\xed\xa0\x80', b'\xf4\x90\x80\x80',
                          b'\xe2\x82', b'\\uD800', b'\\q', b'\\u0000', b'\\u0000suffix']:
                body = b'{"value":"' + value + b'"}'
                header = b'{"type":"x","data_length":' + str(len(body)).encode() + b'}'
                self.assertNotEqual(decode(header, body)[0], 0)
            for body in [b'{"v":1,"v":2}', b'{"\\u0076":1,"v":2}', b'{"v":1e999}',
                         b'{"v":01}', b'{"v":1.}', b'{"v":.1}', b'{"v":[{"a":1,"a":2}]}',
                         b'{"v":' + b'[' * 20 + b'0' + b']' * 20 + b'}',
                         b'{"v":"' + b'x' * 4096 + b'"}']:
                header = b'{"type":"x","data_length":' + str(len(body)).encode() + b'}'
                self.assertNotEqual(decode(header, body)[0], 0)
            for header, body in [
                (b'{"type":"x","data_length":1}', b'{}'),
                (b'{"type":"x","payload_length":1}', b''),
                (b'{"type":"x","data_length":7}', b'{"v":01'),
                (b'{"type":"x","data_length":13}', b'{"v":1,"v":2}'),
                (b'{"type":"x","data":{"v":"\\u0000"}}', b''),
                (b'{"type":"x","data":{"v":"\xff"}}', b''),
                (b'{"type":"x","data":{"v":1e999}}', b''),
                (b'{"type":"x","data":{"a":{"v":1,"v":2}}}', b''),
                (b'{"type":"x","data_length":2}', b'[]'),
            ]:
                with self.subTest(header=header, body=body):
                    self.assertNotEqual(decode(header, body)[0], 0)


if __name__ == '__main__':
    unittest.main()
