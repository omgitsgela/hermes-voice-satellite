#!/usr/bin/env python3
"""The 48 kHz profile's two rate conversions, proven on the host against the real C.

These matter more than they look. The 48 kHz profile exists to stop destroying the bridge's
TTS: at 16 kHz the playback path low-passes at 7400 Hz and throws away everything above it,
which is exactly the "the voice sounds lower quality than it should be" complaint. So the
up-sampler's job is to carry 7.4-11 kHz content ACROSS, and the decimator's job is to keep
10 kHz content OUT of a 16 kHz voice. A test that only checks sample counts proves neither.

Run:  ./.venv/bin/python -B scripts/test_48k_resample.py
"""
import ctypes as C
import math
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AUDIO_C = os.path.join(ROOT, "firmware", "esp32", "components", "wyoming_audio", "wyoming_audio.c")
AUDIO_H = os.path.join(ROOT, "firmware", "esp32", "components", "wyoming_audio", "wyoming_audio.h")

TAPS = 32
OUT_RATE = 48000
IN_RATE = 22050


class Upsampler(C.Structure):
    _fields_ = [("position", C.c_double), ("consumed", C.c_double),
                ("hist", C.c_int16 * 3), ("ratio", C.c_double)]


class Decimator(C.Structure):
    _fields_ = [("hist", C.c_int16 * (TAPS - 1)), ("phase", C.c_int)]


def build():
    """Compile the real C to a shared object. This is the same object the firmware builds."""
    tmp = tempfile.mkdtemp(prefix="wy48k-")
    so = os.path.join(tmp, "wyoming_audio.so")
    cmd = ["cc", "-O1", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC", "-o", so, AUDIO_C, "-lm"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("compile failed:\n" + p.stderr)
    lib = C.CDLL(so)
    lib.wy_resample_22050_to_48000.argtypes = [
        C.POINTER(Upsampler), C.POINTER(C.c_int16), C.c_size_t,
        C.POINTER(C.c_int16), C.c_size_t]
    lib.wy_resample_22050_to_48000.restype = C.c_size_t
    lib.wy_upsampler_reset.argtypes = [C.POINTER(Upsampler)]
    lib.wy_upsampler_reset.restype = C.c_void_p
    lib.wy_upsampler_reset_at.argtypes = [C.POINTER(Upsampler), C.c_double, C.c_double]
    lib.wy_upsampler_reset_at.restype = C.c_void_p
    lib.wy_decimate_48000_to_16000.argtypes = [
        C.POINTER(Decimator), C.POINTER(C.c_int16), C.c_size_t,
        C.POINTER(C.c_int16), C.c_size_t]
    lib.wy_decimate_48000_to_16000.restype = C.c_size_t
    lib.wy_decimator_reset.argtypes = [C.POINTER(Decimator)]
    lib.wy_decimator_reset.restype = C.c_void_p
    return lib


LIB = build()


def _assert_identical(self, got, want, what):   # module-level helper, called with the TestCase
    """Compare two sample lists without asking unittest to diff 48000 numbers on failure."""
    if got == want:
        return
    if len(got) != len(want):
        self.fail(f"chunking changed the {what} output length: {len(got)} vs {len(want)}")
    diffs = [(i, a, b) for i, (a, b) in enumerate(zip(want, got)) if a != b]
    worst = max(abs(a - b) for _, a, b in diffs)
    self.fail(f"chunking changed the {what} output at {len(diffs)}/{len(want)} samples "
              f"(max |diff| {worst}); first at {diffs[0]}")


def tone(rate, freq, n, amp=20000.0, phase=0.0):
    return [int(round(amp * math.sin(2 * math.pi * freq * (i / rate) + phase))) for i in range(n)]


def to_buf(vals):
    arr = (C.c_int16 * len(vals))(*vals)
    return arr


def upsample(lib, vals, cap=200000):
    u = Upsampler()
    lib.wy_upsampler_reset(C.byref(u))
    out = (C.c_int16 * cap)()
    n = lib.wy_resample_22050_to_48000(C.byref(u), to_buf(vals), len(vals), out, cap)
    return list(out[:n])


def decimate(lib, vals, cap=200000):
    d = Decimator()
    lib.wy_decimator_reset(C.byref(d))
    out = (C.c_int16 * cap)()
    n = lib.wy_decimate_48000_to_16000(C.byref(d), to_buf(vals), len(vals), out, cap)
    return list(out[:n])


def energy_at(signal, rate, freq, skip=200):
    """Goertzel-ish magnitude of one frequency, over the part of the signal past the filter warm-up."""
    s = signal[skip:]
    if not s:
        return 0.0
    re = sum(v * math.cos(2 * math.pi * freq * i / rate) for i, v in enumerate(s))
    im = sum(v * math.sin(2 * math.pi * freq * i / rate) for i, v in enumerate(s))
    return 2.0 * math.hypot(re, im) / len(s)


class Test48kUpsample(unittest.TestCase):
    """22050 -> 48000 must CARRY the high band, which is the entire point of the profile."""

    def test_sample_count_is_the_48k_ratio(self):
        out = upsample(LIB, tone(IN_RATE, 440, 22050))
        # 22050 input samples at 22050 Hz = 1 s -> 48000 output samples at 48000 Hz.
        self.assertGreater(len(out), 47800, f"too few samples for 1 s: {len(out)}")
        self.assertLessEqual(len(out), 48010, f"too many samples for 1 s: {len(out)}")

    def test_high_band_survives(self):
        """A 9 kHz tone must come through with real amplitude. At 16 kHz it is above Nyquist and
        would be destroyed; that difference is why this profile exists."""
        n = 22050
        out = upsample(LIB, tone(IN_RATE, 9000, n))
        e = energy_at(out, OUT_RATE, 9000, skip=400)
        self.assertGreater(e, 12000, f"9 kHz did not survive the up-sample: {e:.0f}")

    def test_low_band_survives(self):
        out = upsample(LIB, tone(IN_RATE, 440, 22050))
        e = energy_at(out, OUT_RATE, 440, skip=400)
        self.assertGreater(e, 12000, f"440 Hz did not survive: {e:.0f}")

    def test_chunking_cannot_change_the_output(self):
        """wy_resampler's documented contract, at the new ratio."""
        n = 22050
        sig = tone(IN_RATE, 1000, n, phase=0.3)
        whole = upsample(LIB, sig)

        u = Upsampler()
        LIB.wy_upsampler_reset(C.byref(u))
        out = (C.c_int16 * 200000)()
        total = 0
        for start in range(0, n, 137):          # deliberately awkward chunking
            piece = sig[start:start + 137]
            # Each chunk must APPEND at offset `total`, not rewrite out[0]. Slicing a ctypes array
            # returns a Python list, so the offset pointer is built with byref's offset argument.
            dest = C.cast(C.byref(out, total * C.sizeof(C.c_int16)), C.POINTER(C.c_int16))
            total += LIB.wy_resample_22050_to_48000(
                C.byref(u), to_buf(piece), len(piece), dest, 200000 - total)
        _assert_identical(self, list(out[:total]), whole, "up-sampled")

    def test_no_fir_means_no_extra_delay(self):
        """The 16 kHz path has a 15.5-sample group delay from its low-pass. The up-sampler must
        not inherit it: its output should be in step with its input, not shifted."""
        sig = [0] * 100 + tone(IN_RATE, 1000, 400)
        out = upsample(LIB, sig)
        # Find the first sample with real amplitude and check it lands where the input's does.
        def first_rise(s, thresh=5000):
            for i, v in enumerate(s):
                if abs(v) > thresh:
                    return i
            return -1
        in_rise = first_rise(sig)
        out_rise = first_rise(out)
        # Input rise at sample i -> at 48 kHz that is i * 48000/22050 output samples later.
        expected = in_rise * OUT_RATE / IN_RATE
        self.assertLess(abs(out_rise - expected), 6.0,
                        f"up-sampler is time-shifted: rise at {out_rise}, expected ~{expected:.0f}")

    def test_local_tones_at_16k_upsample_three_to_one(self):
        """The chime and done tone are rendered at 16 kHz. Through a 48 kHz I2S they must be
        up-sampled 3:1, or the wake tone plays three times too fast and far too high."""
        u = Upsampler()
        LIB.wy_upsampler_reset_at(C.byref(u), 16000.0, 48000.0)
        sig = tone(16000, 523, 4800)          # C5 for 300 ms, one of the documented prompt tones
        out = (C.c_int16 * 20000)()
        n = LIB.wy_resample_22050_to_48000(C.byref(u), to_buf(sig), len(sig), out, 20000)
        self.assertGreater(len(n and list(out[:n]) or []), 14000,
                           f"300 ms of 16 kHz should be ~14400 samples at 48 kHz, got {n}")
        e = energy_at(list(out[:n]), 48000, 523, skip=400)
        self.assertGreater(e, 12000, f"the C5 tone lost its amplitude: {e:.0f}")

    def test_empty_and_null_are_safe(self):
        out = (C.c_int16 * 8)()
        u = Upsampler()
        self.assertEqual(LIB.wy_resample_22050_to_48000(C.byref(u), None, 0, out, 8), 0)
        self.assertEqual(LIB.wy_resample_22050_to_48000(C.byref(u), to_buf([1, 2, 3]), 3, out, 0), 0)


class Test48kDecimate(unittest.TestCase):
    """48000 -> 16000 must REJECT the high band: an unfiltered 3:1 pick folds 10 kHz into
    16000 - 10000 = 6 kHz and puts distortion in the voice."""

    def test_ratio_is_exactly_three_to_one(self):
        out = decimate(LIB, tone(48000, 440, 48000))
        self.assertEqual(len(out), 16000, f"expected exactly 16000 from 48000, got {len(out)}")

    def test_voice_band_passes(self):
        out = decimate(LIB, tone(48000, 440, 48000))
        e = energy_at(out, 16000, 440, skip=200)
        self.assertGreater(e, 12000, f"440 Hz lost in the decimator: {e:.0f}")

    def test_alias_band_is_rejected(self):
        """A 10 kHz tone would alias to 6 kHz if the low-pass were missing. It must be gone."""
        out = decimate(LIB, tone(48000, 10000, 48000))
        alias = energy_at(out, 16000, 6000, skip=200)   # 16000 - 10000
        kept = energy_at(out, 16000, 10000, skip=200)
        self.assertLess(alias, 1500, f"10 kHz folded into the voice band: {alias:.0f}")
        self.assertLess(kept, 1500, f"10 kHz survived into 16 kHz output: {kept:.0f}")

    def test_phase_is_continuous_across_chunks(self):
        """A chunk boundary must not shift which samples survive, or the output stutters at every
        boundary. Feeding the same input in one piece and in awkward pieces must agree exactly."""
        sig = tone(48000, 700, 9600, phase=0.2)
        whole = decimate(LIB, sig)

        d = Decimator()
        LIB.wy_decimator_reset(C.byref(d))
        out = (C.c_int16 * 40000)()
        total = 0
        for start in range(0, len(sig), 251):
            piece = sig[start:start + 251]
            dest = C.cast(C.byref(out, total * C.sizeof(C.c_int16)), C.POINTER(C.c_int16))
            total += LIB.wy_decimate_48000_to_16000(
                C.byref(d), to_buf(piece), len(piece), dest, 40000 - total)
        _assert_identical(self, list(out[:total]), whole, "decimated")

    def test_unity_gain_on_dc(self):
        """A DC input must come out at the same level: the FIR's DC gain is unity by design."""
        sig = [10000] * 48000
        out = decimate(LIB, sig)
        tail = out[200:]
        self.assertGreater(len(tail), 0)
        self.assertGreater(min(tail), 9850, f"DC gain is not unity: min {min(tail)}")
        self.assertLess(max(tail), 10150, f"DC gain is not unity: max {max(tail)}")

    def test_reset_clears_phase_and_history(self):
        sig = tone(48000, 500, 960)
        a = decimate(LIB, sig)
        b = decimate(LIB, sig)     # a fresh decimator
        self.assertEqual(a, b, "a fresh decimator did not reproduce the same output")


if __name__ == "__main__":
    unittest.main(verbosity=2)
