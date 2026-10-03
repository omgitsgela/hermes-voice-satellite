#!/usr/bin/env python3
"""Compile the MCU wire core on the host and verify Python Wyoming interop."""
import ctypes as C
import io
from pathlib import Path
import subprocess
import tempfile
import unittest

from wyoming.event import Event, read_event, write_event

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'firmware/esp32/components/wyoming_wire'
WRITE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.c_size_t)
READ = WRITE


class WireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        library = Path(cls.tmp.name) / 'wire.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared',
                        '-fPIC', str(CORE / 'wyoming_wire.c'), '-o', str(library)], check=True)
        cls.lib = C.CDLL(str(library))
        cls.lib.wy_send.argtypes = [WRITE, C.c_void_p, C.c_char_p, C.c_char_p,
                                   C.c_void_p, C.c_size_t]
        cls.lib.wy_read_header.argtypes = [READ, C.c_void_p, C.c_void_p, C.c_size_t]
        cls.lib.wy_read_exact.argtypes = [READ, C.c_void_p, C.c_void_p, C.c_size_t]

    def test_receive_fragmented_header_and_binary_body(self):
        self.assertTrue(hasattr(self.lib, 'wy_read_header'), 'header receiver missing')
        self.assertTrue(hasattr(self.lib, 'wy_read_exact'), 'body receiver missing')
        self.lib.wy_read_header.argtypes = [READ, C.c_void_p, C.c_void_p, C.c_size_t]
        self.lib.wy_read_exact.argtypes = [READ, C.c_void_p, C.c_void_p, C.c_size_t]
        source = io.BytesIO(b'{"type":"audio-chunk","payload_length":4}\n\x00\xff\n\x01NEXT')

        @READ
        def reader(ctx, target, length):
            chunk = source.read(min(length, 2))
            C.memmove(target, chunk, len(chunk))
            return len(chunk)

        header = C.create_string_buffer(1024)
        self.assertEqual(self.lib.wy_read_header(reader, None, header, 1024), 0)
        self.assertEqual(header.value, b'{"type":"audio-chunk","payload_length":4}')
        payload = C.create_string_buffer(4)
        self.assertEqual(self.lib.wy_read_exact(reader, None, payload, 4), 0)
        self.assertEqual(payload.raw, b'\x00\xff\n\x01')
        self.assertEqual(source.read(), b'NEXT')

    def test_python_writer_frames_receive_without_losing_next_event(self):
        import json
        source = io.BytesIO()
        expected = Event(type='audio-chunk', data={'rate': 16000, 'label': 'café'},
                         payload=b'\x00\xff\n\x01')
        write_event(expected, source)
        write_event(Event(type='audio-stop'), source)
        source.seek(0)

        @READ
        def reader(ctx, target, length):
            chunk = source.read(min(length, 3))
            C.memmove(target, chunk, len(chunk))
            return len(chunk)

        header = C.create_string_buffer(1024)
        self.assertEqual(self.lib.wy_read_header(reader, None, header, 1024), 0)
        fields = json.loads(header.value)
        data = C.create_string_buffer(fields['data_length'])
        self.assertEqual(self.lib.wy_read_exact(reader, None, data, len(data)), 0)
        self.assertEqual(json.loads(data.raw), expected.data)
        payload = C.create_string_buffer(fields['payload_length'])
        self.assertEqual(self.lib.wy_read_exact(reader, None, payload, len(payload)), 0)
        self.assertEqual(payload.raw, expected.payload)
        self.assertEqual(read_event(source).type, 'audio-stop')

    def test_send_utf8_counts_bytes(self):
        wire = bytearray()

        @WRITE
        def writer(ctx, data, length):
            wire.extend(C.string_at(data, length))
            return length

        self.assertEqual(self.lib.wy_send(writer, None, b'info',
                                         '{"name":"café"}'.encode(), None, 0), 0)
        self.assertEqual(read_event(io.BytesIO(wire)).data, {'name': 'café'})

    def test_send_rejects_invalid_and_oversized_before_any_write(self):
        writes = []

        @WRITE
        def writer(ctx, data, length):
            writes.append(length)
            return length

        for event_type, data, size, expected in [
            (b'bad"type', None, 0, -1), (b'', None, 0, -1),
            (b'info', b'x' * 4097, 0, -3),
            (b'audio-chunk', None, 16385, -3),
        ]:
            with self.subTest(event_type=event_type, size=size):
                payload = C.create_string_buffer(16385)
                self.assertEqual(self.lib.wy_send(writer, None, event_type, data,
                                                 payload, size), expected)
        self.assertEqual(writes, [])

    def test_write_zero_negative_or_overreported_fails(self):
        for result in (0, -1, 100000):
            @WRITE
            def writer(ctx, data, length):
                return result
            self.assertEqual(self.lib.wy_send(writer, None, b'info', None, None, 0), -2)

    def test_header_and_body_eof_or_overflow_fails(self):
        for wire, capacity, expected in [(b'abc', 8, -2), (b'12345678\n', 8, -3),
                                         (b'\n', 8, -1), (b'a\x00b\n', 8, -1)]:
            source = io.BytesIO(wire)

            @READ
            def reader(ctx, target, length):
                data = source.read(length)
                C.memmove(target, data, len(data))
                return len(data)

            header = C.create_string_buffer(capacity)
            self.assertEqual(self.lib.wy_read_header(reader, None, header, capacity), expected)
        source = io.BytesIO(b'abc')
        target = C.create_string_buffer(4)
        self.assertEqual(self.lib.wy_read_exact(reader, None, target, 4), -2)
        self.assertEqual(self.lib.wy_read_exact(reader, None, target, 16385), -3)

    def test_send_fragmented_writer_is_python_readable(self):
        self.assertTrue((CORE / 'wyoming_wire.c').exists(), 'MCU wire implementation missing')
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / 'wire.so'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared',
                            '-fPIC', str(CORE / 'wyoming_wire.c'), '-o', str(library)], check=True)
            lib = C.CDLL(str(library))
            lib.wy_send.argtypes = [WRITE, C.c_void_p, C.c_char_p, C.c_char_p,
                                   C.c_void_p, C.c_size_t]
            wire = bytearray()

            @WRITE
            def writer(ctx, data, length):
                count = min(length, 3)
                wire.extend(C.string_at(data, count))
                return count

            payload = C.create_string_buffer(b'\x00\xff\n\x01')
            self.assertEqual(lib.wy_send(writer, None, b'audio-chunk',
                                         b'{"rate":16000,"width":2,"channels":1}',
                                         payload, 4), 0)
            event = read_event(io.BytesIO(wire))
            self.assertEqual(event.type, 'audio-chunk')
            self.assertEqual(event.data, {'rate': 16000, 'width': 2, 'channels': 1})
            self.assertEqual(event.payload, b'\x00\xff\n\x01')


if __name__ == '__main__':
    unittest.main()
