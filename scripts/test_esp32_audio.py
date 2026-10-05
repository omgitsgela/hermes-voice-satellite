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

    def test_downmix_scales_32_bit_full_scale_to_16_bit(self):
        """The XVF3800 I2S stream is 32-bit FULL-SCALE (Seeed's own TFLM / Edge Impulse
        guides convert it with `val = i2s_sample >> 16`), not 16-bit samples padded into
        32-bit slots. So each mono sample is the (L+R)/2 average -- computed in 64 bits so a
        near-extreme pair cannot overflow -- then SCALED down 16 bits with rounding, and the
        int16 clip is only a guard for out-of-range input. Clipping instead of scaling turns
        every real speech sample (well above 2^15 in 32-bit terms) into a sign-only square
        wave: the bridge would get +32767/-32768, not audio."""
        lib = self.library()
        stereo = (C.c_int32 * 12)(
            393216, 786432,        # avg 589824, >>16 = 9 (rounds to 9)
            655360, 1310720,       # avg 983040, >>16 = 15
            16777216, 16777216,    # avg 16777216 (2^24), >>16 = 256
            -33554432, -33554432,  # avg -2^25, >>16 = -512
            201326591, 201326591,  # near 32-bit full scale, >>16 = 3072 (not clipped)
            2147483647, 2147483647)  # 32-bit max: avg still in int64, >>16 = 32767
        out = (C.c_int16 * 6)()
        written = lib.wy_audio_downmix_mono16(stereo, 6, out, 6)
        self.assertEqual(written, 6)
        self.assertEqual(list(out), [9, 15, 256, -512, 3072, 32767])

    def test_downmix_32_bit_near_full_scale_is_not_a_square_wave(self):
        """A 32-bit value at ordinary speech level must map into the int16 range, not pin to
        the top: this is the regression that clipping (instead of >>16) would fail."""
        lib = self.library()
        # 1,000,000 in 32-bit full-scale is roughly -26 dBFS: comfortably audible speech.
        stereo = (C.c_int32 * 2)(1000000, 1000000)
        out = (C.c_int16 * 1)()
        lib.wy_audio_downmix_mono16(stereo, 1, out, 1)
        self.assertEqual(out[0], 15, 'speech-level 32-bit sample scales, it does not clip to 32767')

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


    def resampler(self):
        """A wy_resampler is a struct {double position}; the ctypes view and the reset/feed
        bindings. The position is carried across feed() calls, so consecutive chunks resample
        continuously."""
        lib = self.library()
        class Resampler(C.Structure):
            _fields_ = [('position', C.c_double), ('consumed', C.c_double),
                        ('prev_value', C.c_int32)]
        lib.wy_resampler_reset.argtypes = [C.POINTER(Resampler)]
        lib.wy_resampler_reset.restype = None
        lib.wy_resample_22050_to_16000.argtypes = [C.POINTER(Resampler), C.POINTER(C.c_int16),
                                                   C.c_size_t, C.POINTER(C.c_int16), C.c_size_t]
        lib.wy_resample_22050_to_16000.restype = C.c_size_t
        return lib, Resampler

    def test_resample_one_second_is_one_second(self):
        """One second of 22050 Hz input yields ~one second of 16000 Hz output. The exact count
        is set by the fractional cursor, so allow +/-1 around the theoretical 16000."""
        lib, R = self.resampler()
        n = 22050
        ramp = (C.c_int16 * n)()
        for i in range(n):
            ramp[i] = i % 2000   # a bounded ramp (stays well inside int16)
        out = (C.c_int16 * 16005)()
        r = R(); lib.wy_resampler_reset(C.byref(r))
        written = lib.wy_resample_22050_to_16000(C.byref(r), ramp, n, out, 16005)
        self.assertTrue(15999 <= written <= 16001,
                        f'one second in should give ~one second out, got {written}')

    def test_resample_is_continuous_across_chunk_boundary(self):
        """A linear ramp resamples to the same values whether fed as one chunk or as two halves:
        the fractional cursor carries across the seam, so there is no step at the boundary. A
        chunked resampler that resets its cursor (or drops/dues a sample at the seam) fails this."""
        lib, R = self.resampler()
        n = 22050
        ramp = (C.c_int16 * n)()
        for i in range(n):
            ramp[i] = i          # the identity ramp: resampling it is just the cursor, so the
                                 # output depends only on the cursor, not the chunking
        # one-shot
        out1 = (C.c_int16 * 16005)()
        r1 = R(); lib.wy_resampler_reset(C.byref(r1))
        c1 = lib.wy_resample_22050_to_16000(C.byref(r1), ramp, n, out1, 16005)
        # the same total input, fed as many small chunks with the absolute offset carried:
        # the resampler is chunking-invariant, so this must equal the one-shot resample
        chunk = 2048   # the bridge's samples_per_chunk shape
        offs = range(0, n, chunk)
        # Each chunk emits at most ceil(2048 * 16000/22050) + 1 samples, so a 2048-slot buffer
        # per chunk is enough; the values are concatenated in stream order.
        r2 = R(); lib.wy_resampler_reset(C.byref(r2))
        parts = []
        for o in offs:
            m = min(chunk, n - o)
            piece = (C.c_int16 * m)()          # the chunk's own array (in[0] = first new sample)
            for i in range(m):
                piece[i] = ramp[o + i]
            buf = (C.c_int16 * 1600)()         # m<=2048 samples -> at most ~1489 out samples
            k = lib.wy_resample_22050_to_16000(C.byref(r2), piece, m, buf, 1600)
            parts.extend(buf[:k])
        chunked = parts
        oneshot = list(out1[:c1])
        m = min(len(chunked), len(oneshot))
        self.assertGreater(m, 15900)
        self.assertEqual(chunked[:m], oneshot[:m],
                         'resampling the same ramp in chunks must equal one chunk (absolute cursor)')

    def test_resample_full_scale_stays_in_range(self):
        """Alternating full-scale input stays in the int16 range (no wrap, no overflow)."""
        lib, R = self.resampler()
        n = 22050
        in_ = (C.c_int16 * n)()
        for i in range(n):
            in_[i] = 32767 if (i % 2 == 0) else -32768
        out = (C.c_int16 * 16005)()
        r = R(); lib.wy_resampler_reset(C.byref(r))
        written = lib.wy_resample_22050_to_16000(C.byref(r), in_, n, out, 16005)
        self.assertGreater(written, 0)
        for i in range(written):
            self.assertTrue(-32768 <= out[i] <= 32767)

    def test_resample_empty_and_reset(self):
        lib, R = self.resampler()
        r = R(); lib.wy_resampler_reset(C.byref(r))
        out = (C.c_int16 * 4)()
        self.assertEqual(lib.wy_resample_22050_to_16000(C.byref(r), None, 0, out, 4), 0)
        # after a chunk, the stored absolute position is past the chunk; reset puts it back to 0
        in_ = (C.c_int16 * 22050)()
        lib.wy_resample_22050_to_16000(C.byref(r), in_, 22050, out, 4)
        self.assertGreater(r.position, 0.0)
        lib.wy_resampler_reset(C.byref(r))
        self.assertEqual(r.position, 0.0)


    def volume(self):
        lib = self.library()
        lib.wy_apply_volume.argtypes = [C.POINTER(C.c_int16), C.c_size_t, C.c_int]
        lib.wy_apply_volume.restype = C.c_size_t
        return lib

    def test_volume_never_reaches_silence(self):
        """The project rule: no path may take the device to silence. A request of 0 (or a
        negative or otherwise bad value) must become the floor, a small NON-zero gain -- never
        zeros the stream. That is the deaf-device regression the floor exists to catch."""
        lib = self.volume()
        for bad in (0, -5, 1000):
            with self.subTest(bad=bad):
                pcm = (C.c_int16 * 4)(8000, -8000, 12000, -12000)
                lib.wy_apply_volume(pcm, 4, bad)
                self.assertNotEqual((pcm[0], pcm[1]), (0, 0), 'a bad value must not silence')
                self.assertGreater(abs(pcm[0]), 0)
        # and 0 and the floor agree: both land on the floor gain
        a = (C.c_int16 * 1)(10000); b = (C.c_int16 * 1)(10000)
        lib.wy_apply_volume(a, 1, 0)
        lib.wy_apply_volume(b, 1, 1)   # the floor percent (WY_VOLUME_FLOOR_PERCENT)
        # 1 is below a 25% floor, so it clamps to the floor too: 0 -> floor, 1 -> floor
        self.assertEqual(a[0], b[0], '0 and a sub-floor value both become the floor')
        self.assertGreater(a[0], 0)

    def test_volume_100_is_identity_and_50_halves(self):
        lib = self.volume()
        pcm = (C.c_int16 * 3)(32767, -32768, 12345)
        lib.wy_apply_volume(pcm, 3, 100)
        self.assertEqual(list(pcm), [32767, -32768, 12345], '100 percent is the identity')
        pcm2 = (C.c_int16 * 3)(32767, -32768, 12345)
        lib.wy_apply_volume(pcm2, 3, 50)
        self.assertEqual(pcm2[0], 16383, '50 percent halves (rounded)')
        self.assertEqual(pcm2[1], -16384)
        self.assertIn(pcm2[2], (6172, 6173), '50 percent halves a mid value (±1 for rounding)')

    def test_volume_stays_in_range_and_is_monotonic(self):
        """Full-scale input at any permitted level stays in the int16 range, and a higher level
        gives a louder (or equal) magnitude: monotonic, so 'volume up' never gets quieter."""
        lib = self.volume()
        pcm = (C.c_int16 * 1)(32767)
        lib.wy_apply_volume(pcm, 1, 25)
        self.assertTrue(-32768 <= pcm[0] <= 32767)
        lo = (C.c_int16 * 1)(16000); hi = (C.c_int16 * 1)(16000)
        lib.wy_apply_volume(lo, 1, 30)
        lib.wy_apply_volume(hi, 1, 70)
        self.assertGreaterEqual(hi[0], lo[0], 'a higher level must not be quieter')
        # empty is a no-op
        self.assertEqual(lib.wy_apply_volume(None, 0, 50), 0)


    def expand(self):
        lib = self.library()
        lib.wy_expand_mono16_to_stereo32.argtypes = [C.POINTER(C.c_int16), C.c_size_t,
                                                    C.POINTER(C.c_int32), C.c_size_t]
        lib.wy_expand_mono16_to_stereo32.restype = C.c_size_t
        return lib

    def test_expand_is_stereo_full_scale(self):
        """A mono 16 kHz S16 sample becomes the XVF's 32-bit FULL-SCALE form on BOTH channels:
        +1234 -> 1234 << 16 in L and R. The I2S TX slot is stereo 32-bit, so each mono sample
        yields two int32 (L=R). Clipping a 16-bit sample into a 16-bit slot would be wrong --
        the XVF's own 32-bit full-scale convention (the downmix's inverse) is the contract."""
        lib = self.expand()
        mono = (C.c_int16 * 3)(1234, -1234, 0)
        out = (C.c_int32 * 6)()
        written = lib.wy_expand_mono16_to_stereo32(mono, 3, out, 6)
        self.assertEqual(written, 6)
        self.assertEqual(list(out), [1234 << 16, 1234 << 16,
                                     -1234 << 16, -1234 << 16, 0, 0])

    def test_expand_downmix_round_trip(self):
        """expand is the exact inverse of the capture downmix: downmix(expand(x)) == x for in-range
        x. This pins the 32-bit full-scale convention in both directions at once."""
        lib = self.library()
        lib.wy_expand_mono16_to_stereo32.argtypes = [C.POINTER(C.c_int16), C.c_size_t,
                                                    C.POINTER(C.c_int32), C.c_size_t]
        lib.wy_expand_mono16_to_stereo32.restype = C.c_size_t
        mono = (C.c_int16 * 64)()
        import random
        random.seed(7)
        for i in range(64):
            mono[i] = random.randint(-32768, 32767)
        out = (C.c_int32 * 128)()
        self.assertEqual(lib.wy_expand_mono16_to_stereo32(mono, 64, out, 128), 128)
        back = (C.c_int16 * 64)()
        lib.wy_audio_downmix_mono16(out, 64, back, 64)
        self.assertEqual(list(back), list(mono), 'downmix(expand(x)) must equal x')

    def test_expand_respects_capacity(self):
        """A buffer too small for all the frames writes nothing and reports 0 (atomic, so a
        caller never sees a half-expanded frame)."""
        lib = self.expand()
        mono = (C.c_int16 * 4)(1, 2, 3, 4)
        out = (C.c_int32 * 6)()   # room for only 3 frames
        self.assertEqual(lib.wy_expand_mono16_to_stereo32(mono, 4, out, 6), 0)
        self.assertEqual(list(out), [0] * 6, 'an undersized buffer must not be partially written')
        # exactly the right size is fine
        out2 = (C.c_int32 * 8)()
        self.assertEqual(lib.wy_expand_mono16_to_stereo32(mono, 4, out2, 8), 8)


    def amp_encoder(self):
        lib = self.library()
        lib.wy_amp_gpo_command.argtypes = [C.POINTER(C.c_uint8), C.c_int]
        lib.wy_amp_gpo_command.restype = None
        return lib

    def test_amp_gpo_command_is_byte_exact(self):
        """The amplifier is an XMOS GPO (X0D31), NOT an ESP32 GPIO: it is written over the XVF's
        I2C GPO Servicer. GPO_WRITE_VALUE = ResID 20, CmdID 1, payload [pin, level] = [31, level],
        and the I2C write form is one 5-byte transaction [resid, cmd, count, p0, p1]. The encoder
        must emit exactly those bytes -- the device glue is a thin transmit of known-correct
        bytes, so a wrong constant here is a wrong pin, not a harmless tweak."""
        lib = self.amp_encoder()
        buf = (C.c_uint8 * 5)()
        lib.wy_amp_gpo_command(buf, 1)   # enabled
        self.assertEqual(list(buf), [20, 1, 2, 31, 0],
                         'enable: GPO_WRITE_VALUE [resid,cmd,count,pin,level] with active-low LOW')
        lib.wy_amp_gpo_command(buf, 0)   # disabled
        self.assertEqual(list(buf), [20, 1, 2, 31, 1],
                         'disable: the same transaction, level HIGH (active-low)')

    def test_amp_gpo_enable_is_active_low(self):
        """X0D31 is active-LOW: LOW (0) ENABLES the amp, HIGH (1) disables it. The mapping is the
        whole safety of the 'arm the amp to play' path -- a swapped mapping would mute every
        answer. Pinned in both directions."""
        lib = self.amp_encoder()
        on = (C.c_uint8 * 5)(); off = (C.c_uint8 * 5)()
        lib.wy_amp_gpo_command(on, 1)
        lib.wy_amp_gpo_command(off, 0)
        self.assertEqual(on[4], 0, 'enabled -> level 0 (LOW, active-low)')
        self.assertEqual(off[4], 1, 'disabled -> level 1 (HIGH)')
        # everything else about the command is identical
        self.assertEqual(list(on[:4]), list(off[:4]))


if __name__ == '__main__':
    unittest.main()
