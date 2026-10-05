#!/usr/bin/env python3
"""Audio-capture host tests.

Prove the portable half of capture against the canonical Wyoming implementation in both
directions the project standard requires: the device's mic `audio-chunk`, sent with the
production `wy_send`, reads back in Python `AudioChunk.from_event` with the exact
rate/width/channels and PCM payload, and the 32-bit stereo -> 16-bit mono downmix is exact
and bounded.

The I2S read itself is device-only (ESP-IDF); it is proven by the firmware build + ELF
symbol, not here.
"""
import ctypes as C
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from wyoming.audio import AudioChunk
from wyoming.event import read_event

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / 'firmware/esp32/components'
IO = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.c_size_t)


class AudioTests(unittest.TestCase):
    def library(self):
        audio = COMP / 'wyoming_audio/wyoming_audio.c'
        wire = COMP / 'wyoming_wire/wyoming_wire.c'
        self.assertTrue(audio.exists(), 'audio core missing')
        self.assertTrue(wire.exists(), 'wire core missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'audio.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        '-I' + str(COMP / 'wyoming_wire'), '-I' + str(COMP / 'wyoming_audio'),
                        str(wire), str(audio), '-lm', '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.wy_audio_chunk_data.argtypes = [C.c_char_p, C.c_size_t,
                                            C.c_int, C.c_int, C.c_int]
        lib.wy_audio_chunk_data.restype = C.c_int
        lib.wy_audio_downmix_mono16.argtypes = [C.POINTER(C.c_int32), C.c_size_t,
                                                C.POINTER(C.c_int16), C.c_size_t]
        lib.wy_audio_downmix_mono16.restype = C.c_int
        lib.wy_send.argtypes = [IO, C.c_void_p, C.c_char_p, C.c_char_p, C.c_void_p, C.c_size_t]
        lib.wy_send.restype = C.c_int
        return lib

    def test_embedded_writer_chunk_reads_in_canonical_reader(self):
        """The device's mic chunk, sent with the production `wy_send`, must read back in the
        bridge's `AudioChunk.from_event` with the exact rate/width/channels and PCM payload.
        A self-round-trip would let the same defect survive on both sides; this is interop."""
        lib = self.library()
        pcm = bytes(range(256)) * 4          # 1024 bytes: distinct, order-revealing
        data = C.create_string_buffer(64)
        n = lib.wy_audio_chunk_data(data, 64, 16000, 2, 1)
        self.assertGreater(n, 0)
        self.assertLess(n, 64)
        output = io.BytesIO()

        @IO
        def writer(ctx, target, length):
            output.write(C.string_at(target, length))
            return length

        # One mic chunk: 512 mono samples, 16-bit little-endian, 16 kHz. `data` is NUL-terminated,
        # so hand the whole buffer to wy_send (it scans the data field to its NUL).
        self.assertEqual(lib.wy_send(writer, None, b'audio-chunk',
                                     data.raw, pcm, len(pcm)), 0)
        output.seek(0)
        event = read_event(output)
        self.assertIsNotNone(event)
        self.assertEqual(event.type, 'audio-chunk')
        chunk = AudioChunk.from_event(event)
        self.assertEqual((chunk.rate, chunk.width, chunk.channels), (16000, 2, 1))
        self.assertEqual(chunk.audio, pcm, 'the PCM payload must survive byte-for-byte')
        self.assertEqual(chunk.samples, len(pcm) // 2)

    def test_chunk_data_carries_exactly_the_format_fields(self):
        """The chunk data carries rate/width/channels, and nothing the bridge would distrust."""
        lib = self.library()
        data = C.create_string_buffer(64)
        n = lib.wy_audio_chunk_data(data, 64, 22050, 2, 1)
        self.assertGreater(n, 0)
        parsed = json.loads(data.raw[:n].decode('utf-8'))
        self.assertEqual(parsed, {'rate': 22050, 'width': 2, 'channels': 1})

    def test_chunk_data_rejects_bad_format(self):
        lib = self.library()
        data = C.create_string_buffer(64)
        for rate, width, channels in [(0, 2, 1), (16000, 0, 1), (16000, 2, 0),
                                      (16000, -1, 1), (16000, 2, 3)]:
            with self.subTest(rate=rate, width=width, channels=channels):
                self.assertLess(lib.wy_audio_chunk_data(data, 64, rate, width, channels), 0)

    def test_chunk_data_never_overflows(self):
        """A tiny buffer is refused, not written over."""
        lib = self.library()
        data = C.create_string_buffer(4)
        self.assertLess(lib.wy_audio_chunk_data(data, 4, 16000, 2, 1), 0)

    def test_downmix_averages_and_clips_to_int16(self):
        """32-bit stereo in, 16-bit mono out: each sample is the (L+R)/2 average, clipped to
        the int16 range rather than overflowing."""
        lib = self.library()
        stereo = (C.c_int32 * 10)(
            1000, 2000,            # avg 1500
            -4000, -2000,          # avg -3000
            30000000, 30000000,    # avg 30000000 -> clip to +32767
            -30000000, -30000000,  # avg -30000000 -> clip to -32768
            1, 1001)               # avg 501
        out = (C.c_int16 * 5)()
        written = lib.wy_audio_downmix_mono16(stereo, 5, out, 5)
        self.assertEqual(written, 5)
        self.assertEqual(list(out), [1500, -3000, 32767, -32768, 501])

    def test_downmix_respects_capacity(self):
        """A short buffer yields as many samples as fit, never one over."""
        lib = self.library()
        stereo = (C.c_int32 * 6)(0, 0, 0, 0, 100, 100)
        out = (C.c_int16 * 2)()
        written = lib.wy_audio_downmix_mono16(stereo, 3, out, 2)
        self.assertEqual(written, 2)
        self.assertEqual(list(out), [0, 0])

    def test_downmix_empty_capacity(self):
        lib = self.library()
        stereo = (C.c_int32 * 2)(1, 1)
        self.assertEqual(lib.wy_audio_downmix_mono16(stereo, 1, None, 0), 0)


if __name__ == '__main__':
    unittest.main()
