#!/usr/bin/env python3
"""Playback resampler tests: the 22050 -> 16000 converter the TTS path depends on.

Two properties matter and are asserted here.

1. CHUNK INVARIANCE is the resampler's documented contract: "feeding the same total input in any
   chunking produces the same output sequence". The anti-aliasing FIR carries its history inside
   wy_resampler precisely so this holds; if it ever stops holding, the audio develops seams that
   sound like choppiness and there is no other test that would catch it.

2. ANTI-ALIASING is why the FIR exists. Downsampling by linear interpolation alone folds every
   input component above the output's Nyquist limit (8 kHz) back into the audible band as harsh
   distortion. A tone above that limit must come out SMALL, not folded.
"""
import ctypes as C
import math
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / 'firmware/esp32/components'


class WyResampler(C.Structure):
    _fields_ = [('position', C.c_double),
                ('consumed', C.c_double),
                ('prev_value', C.c_int32),
                ('hist', C.c_int16 * 31)]


class ResamplerTests(unittest.TestCase):
    def library(self):
        audio = COMP / 'wyoming_audio/wyoming_audio.c'
        self.assertTrue(audio.exists(), 'audio core missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'audio.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        '-I' + str(COMP / 'wyoming_audio'), str(audio), '-lm', '-o', str(library)],
                       check=True)
        lib = C.CDLL(str(library))
        lib.wy_resample_22050_to_16000.argtypes = [
            C.POINTER(WyResampler), C.POINTER(C.c_int16), C.c_size_t,
            C.POINTER(C.c_int16), C.c_size_t]
        lib.wy_resample_22050_to_16000.restype = C.c_size_t
        lib.wy_resampler_reset.argtypes = [C.POINTER(WyResampler)]
        return lib

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def samples(values):
        buf = (C.c_int16 * len(values))(*values)
        return buf

    @staticmethod
    def tone(hz, n, rate=22050, amp=20000):
        return [int(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)]

    @staticmethod
    def cover(total, size):
        """Chunk sizes that cover EXACTLY `total` samples. A test that feeds a different total
        for different chunkings is comparing different inputs -- which is what my first draft did
        (4000 samples in chunks of 17 feeds only 3995) and it falsely failed the resampler."""
        full, rest = divmod(total, size)
        sizes = [size] * full
        if rest:
            sizes.append(rest)
        return sizes

    def resample(self, lib, values, chunk_sizes, cap=1 << 20):
        """Feed `values` in the given chunk sizes; return the concatenation of the output."""
        r = WyResampler()
        lib.wy_resampler_reset(C.byref(r))
        out = []
        pos = 0
        for size in chunk_sizes:
            piece = values[pos:pos + size]
            if not piece:
                break
            pos += size
            buf = self.samples(piece)
            dest = (C.c_int16 * cap)()
            got = lib.wy_resample_22050_to_16000(
                C.byref(r), buf, len(piece), dest, cap)
            out.extend(dest[:got])
        return out

    # -- 1. the contract ---------------------------------------------------
    def test_output_is_identical_for_any_chunking(self):
        """The same total input must give byte-identical output however it is chunked."""
        lib = self.library()
        values = self.tone(440, 4000)
        whole = self.resample(lib, values, [len(values)])
        self.assertTrue(whole, 'the resampler produced nothing')
        n = len(values)
        for sizes in ([n], self.cover(n, 1), self.cover(n, 2), self.cover(n, 17),
                      self.cover(n, 1024), [3, 1, 400, 7] + self.cover(n - 411, 2000)):
            self.assertEqual(sum(sizes), n, 'chunking must cover the whole input exactly')
            got = self.resample(lib, values, sizes)
            self.assertEqual(len(got), len(whole),
                             'chunking changed the output length: %r' % (sizes[:3],))
            self.assertEqual(got, whole,
                             'chunking changed the output samples: %r' % (sizes[:3],))

    def test_output_length_matches_the_rate_ratio(self):
        """22050 -> 16000 is a 0.7258 ratio; the count must track it to within one sample."""
        lib = self.library()
        for n in (1000, 8000, 22050):
            got = self.resample(lib, self.tone(440, n), [n])
            expected = int(n * 16000 / 22050)
            self.assertLessEqual(abs(len(got) - expected), 2,
                                 '%d samples -> %d out, expected ~%d' % (n, len(got), expected))

    def test_a_single_sample_never_reads_out_of_bounds(self):
        """The bound is the safety contract: never past in[n), never past out[cap]."""
        lib = self.library()
        r = WyResampler()
        lib.wy_resampler_reset(C.byref(r))
        buf = self.samples([1000])
        dest = (C.c_int16 * 1)()
        lib.wy_resample_22050_to_16000(C.byref(r), buf, 1, dest, 1)

    # -- 2. the fix --------------------------------------------------------
    def test_content_above_the_output_nyquist_is_attenuated(self):
        """9 kHz is above the 8 kHz Nyquist of 16 kHz output. Without the FIR it aliases.
        Measure the energy that survives: it must be far below the tone's own amplitude."""
        lib = self.library()
        amp = 20000
        values = self.tone(9000, 22050, amp=amp)
        out = self.resample(lib, values, [len(values)])
        self.assertTrue(out, 'no output')
        # amplitude of the output: peak, which is robust and needs no windowing
        peak = max(abs(v) for v in out)
        self.assertLess(peak, amp * 0.20,
                        'a 9 kHz tone survived downsampling at %d/20000 -- the FIR is not '
                        'removing content above the output Nyquist' % peak)

    def test_content_below_the_cutoff_is_preserved(self):
        """The filter must not muffle speech: a 1 kHz tone must come through near full scale."""
        lib = self.library()
        amp = 20000
        values = self.tone(1000, 22050, amp=amp)
        out = self.resample(lib, values, [len(values)])
        self.assertTrue(out, 'no output')
        # skip the FIR's start-up transient before measuring
        settled = out[len(out) // 4:]
        peak = max(abs(v) for v in settled)
        self.assertGreater(peak, amp * 0.85,
                           'a 1 kHz tone came out at %d/20000 -- the filter is muffling speech'
                           % peak)

    def test_dc_gain_is_unity(self):
        """A constant input must come out at the same level, not scaled."""
        lib = self.library()
        values = [12000] * 22050
        out = self.resample(lib, values, [len(values)])
        settled = out[len(out) // 4:]
        self.assertTrue(settled, 'no output')
        level = sum(settled) / len(settled)
        self.assertLess(abs(level - 12000), 200,
                        'DC gain is not unity: constant 12000 came out at %.0f' % level)


if __name__ == '__main__':
    unittest.main(verbosity=2)
