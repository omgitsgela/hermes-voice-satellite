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
import re
import struct
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
        layout = Path(temporary.name) / 'layout.c'
        layout.write_text('#include "wyoming_audio.h"\n'
                          'size_t test_resampler_size(void) { return sizeof(wy_resampler); }\n')
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        '-I' + str(COMP / 'wyoming_wire'), '-I' + str(COMP / 'wyoming_audio'),
                        str(wire), str(audio), str(layout), '-lm', '-o', str(library)], check=True)
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
        """Mirror the production FIR resampler and verify its ABI before any C write."""
        lib = self.library()
        class Resampler(C.Structure):
            _fields_ = [('position', C.c_double), ('consumed', C.c_double),
                        ('prev_value', C.c_int32), ('hist', C.c_int16 * 31)]
        lib.test_resampler_size.restype = C.c_size_t
        self.assertEqual(C.sizeof(Resampler), lib.test_resampler_size(),
                         'ctypes resampler allocation does not match production C')
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

    def test_volume_is_a_user_scale_spread_over_the_usable_range(self):
        """The level is a USER level (1..100), mapped onto the gain range, not a raw gain percent.

        This test used to assert that 50 halved the signal, i.e. that the user number WAS the gain.
        That was the bug: with a 25 percent floor, every user value at or below 25 collapsed into
        one identical loudness, so a quarter of the scale did nothing and the numbers a person says
        did not match the volume they heard. The Pi satellite spreads its user scale over the usable
        range; the ESP32 now does the same, so a level means the same thing on either device.

        50 maps to 25 + round(49 * 75 / 99) = 62 percent.
        """
        lib = self.volume()
        lib.wy_volume_user_to_gain_percent.argtypes = [C.c_int]
        lib.wy_volume_user_to_gain_percent.restype = C.c_int
        pcm = (C.c_int16 * 3)(32767, -32768, 12345)
        lib.wy_apply_volume(pcm, 3, 100)
        self.assertEqual(list(pcm), [32767, -32768, 12345], '100 is the identity')
        pcm2 = (C.c_int16 * 3)(32767, -32768, 12345)
        lib.wy_apply_volume(pcm2, 3, 50)
        self.assertEqual(lib.wy_volume_user_to_gain_percent(50), 62)
        self.assertEqual(pcm2[0], 20316)   # 32767 * 0.62, rounded half away from zero
        self.assertEqual(pcm2[1], -20316)
        self.assertEqual(pcm2[2], 7654)

    def test_every_user_step_is_a_real_step(self):
        """No dead zones: each user level 1..100 must map to a distinct gain, monotonically.

        The failure this pins is subtle and was live on the device: asking for 5, 10, 20 or 25 gave
        exactly the same loudness, so the lower quarter of the control felt broken.
        """
        lib = self.volume()
        lib.wy_volume_user_to_gain_percent.argtypes = [C.c_int]
        lib.wy_volume_user_to_gain_percent.restype = C.c_int
        gains = [lib.wy_volume_user_to_gain_percent(u) for u in range(1, 101)]
        self.assertEqual(gains[0], 25, 'user 1 is the audible floor')
        self.assertEqual(gains[-1], 100, 'user 100 is full scale')
        self.assertEqual(gains, sorted(gains), 'the mapping must never go backwards')
        # 100 user levels onto 76 integer gains cannot be one-to-one, and pretending otherwise
        # would be an assertion about arithmetic rather than about the device. What matters is that
        # the low end is not a dead zone and the scale is finely resolved throughout.
        self.assertGreaterEqual(len(set(gains)), 70, 'the scale must stay finely resolved')
        low = [lib.wy_volume_user_to_gain_percent(u) for u in (5, 10, 20, 25)]
        self.assertEqual(len(set(low)), len(low), '5, 10, 20 and 25 must not be the same loudness')

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
        # quiet samples must not be truncated to zero by the scaling: 1 * 50% = 0.5 rounds to 1
        quiet = (C.c_int16 * 1)(1)
        lib.wy_apply_volume(quiet, 1, 50)
        self.assertEqual(quiet[0], 1, 'half a sample rounds to a sample, it is not truncated away')
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

    def scalar_encoder(self):
        lib = self.library()
        lib.wy_control_i32_command.argtypes = [C.POINTER(C.c_uint8), C.c_uint8, C.c_uint8,
                                               C.c_int32]
        lib.wy_control_i32_command.restype = None
        lib.wy_control_f32_command.argtypes = [C.POINTER(C.c_uint8), C.c_uint8, C.c_uint8,
                                               C.c_float]
        lib.wy_control_f32_command.restype = None
        return lib

    def test_scalar_control_count_is_the_payload_length(self):
        """The count field is the PAYLOAD LENGTH IN BYTES, so a 32-bit value sends 4.

        This test previously asserted count=1 and was WRONG, in confident prose. Measured on the
        board on 2026-10-29 with count=1: the co-processor ACKs and applies nothing. Every scalar
        control was silently inert for as long as this said 1 -- mic gain, ASR gain, and the PP AGC
        alike -- which is why "the gain controls do nothing" looked like a hardware limitation.

        With count=4 the same value takes effect immediately: AEC_ASROUTGAIN 1.0 -> 32.0 moved the
        measured room level from p90 58 to p90 1140, a 30-40x jump that matches the 32x requested.
        An ACK is only a bus acknowledgement, so the count byte has to be right."""
        lib = self.scalar_encoder()
        buf = (C.c_uint8 * 7)()
        lib.wy_control_i32_command(buf, 33, 35, 1)      # AEC_ASROUTONOFF = ASR tap
        self.assertEqual(list(buf), [33, 35, 4, 1, 0, 0, 0],
                         'count must be the payload length (4) and the value little-endian')
        lib.wy_control_i32_command(buf, 33, 35, 0)
        self.assertEqual(list(buf), [33, 35, 4, 0, 0, 0, 0])

    def test_scalar_float_is_ieee754_little_endian(self):
        """1.0f is 0x3F800000, so the bytes on the wire are 00 00 80 3F. Casting the value
        through an integer instead would put 01 00 00 00 on the wire and set a completely
        different number -- a mistake that looks like a successful write."""
        lib = self.scalar_encoder()
        buf = (C.c_uint8 * 7)()
        lib.wy_control_f32_command(buf, 33, 36, 1.0)      # AEC_ASROUTGAIN
        self.assertEqual(list(buf), [33, 36, 4, 0x00, 0x00, 0x80, 0x3F])
        lib.wy_control_f32_command(buf, 17, 11, 30.0)
        got = struct.unpack('<f', bytes(buf[3:7]))[0]
        self.assertAlmostEqual(got, 30.0, places=4, msg='AGC max gain round-trips exactly')

    def route_encoder(self):
        lib = self.library()
        lib.wy_output_route_command.argtypes = [C.POINTER(C.c_uint8), C.c_int, C.c_int, C.c_int]
        lib.wy_output_route_command.restype = None
        lib.wy_output_upsample_command.argtypes = [C.POINTER(C.c_uint8), C.c_int, C.c_int]
        lib.wy_output_upsample_command.restype = None
        return lib

    def test_output_route_command_is_byte_exact(self):
        """The DAC plays whatever the co-processor's output mux selects, so the routing bytes ARE
        the audible source. AUDIO_MGR_OP_L = ResID 35 / CmdID 15, OP_R = CmdID 19, payload
        [category, source], one 5-byte transaction. Pinned for the two paths playback uses."""
        lib = self.route_encoder()
        buf = (C.c_uint8 * 5)()
        lib.wy_output_route_command(buf, 0, 4, 0)     # left, rate-converted far end
        self.assertEqual(list(buf), [35, 15, 2, 4, 0], 'OP_L: [35,15,count=2,cat,src]')
        lib.wy_output_route_command(buf, 1, 10, 0)    # right, native-rate far end
        self.assertEqual(list(buf), [35, 19, 2, 10, 0], 'OP_R selects CmdID 19, not 15')
        lib.wy_output_route_command(buf, 0, 10, 0)    # left, native-rate far end
        self.assertEqual(list(buf), [35, 15, 2, 10, 0], 'native-rate category is 10')

    def test_output_upsample_command_clears_both_flags(self):
        """AUDIO_MGR_OP_UPSAMPLE is ResID 35 / CmdID 14 with payload [L, R], and in a 48 kHz
        interface configuration the chip DEFAULTS both flags to 1 -- which rate-converts our
        already-48 kHz playback down to 16 kHz and back before it reaches the DAC, band-limiting
        answers to 8 kHz and cancelling the entire reason the 48 kHz profile exists.

        So the command the firmware must send is specifically the one that CLEARS both flags, and
        the byte that says so is payload bytes 3 and 4. A test that only checked 'a write happened'
        would pass with the flags left on, which is the bug."""
        lib = self.route_encoder()
        cleared = (C.c_uint8 * 5)()
        lib.wy_output_upsample_command(cleared, 0, 0)
        self.assertEqual(list(cleared), [35, 14, 2, 0, 0],
                         'the full-band setting is [35,14,count=2,L=0,R=0]')
        default = (C.c_uint8 * 5)()
        lib.wy_output_upsample_command(default, 1, 1)
        self.assertEqual(list(default), [35, 14, 2, 1, 1],
                         'the chip default is distinguishable from our setting only in payload')
        # the two differ ONLY in the two flag bytes -- proof that a wrong flag is silent, not loud
        self.assertEqual(list(cleared[:3]), list(default[:3]),
                         'framing is identical; only the flags carry the whole difference')

    def test_native_rate_category_differs_from_converted_category(self):
        """Category 4 (far end, post rate conversion to 16 kHz) and category 10 (far end at the
        native interface rate) are different mux taps and must not be conflated: 4 is the
        band-limited path, 10 is the full-band path on a 48 kHz link.

        This asserts on the CONSTANTS THE FIRMWARE ACTUALLY USES, not on literals passed into the
        encoder. An earlier version of this test called the encoder with hard-coded 4 and 10 and
        therefore passed even when the header's category constant was changed to something else --
        it tested the encoder's arithmetic and not the value the device sends."""
        import re
        header = (COMP / 'wyoming_audio/wyoming_audio.h').read_text()
        def macro(name):
            m = re.search(rf'^\s*#define\s+{name}\s+(\d+)', header, re.M)
            if m is None:
                self.fail(f'{name} is not defined in wyoming_audio.h')
            return int(m.group(1))
        self.assertEqual(macro('XVF_I2C_CAT_FAR_END'), 4, 'far end, rate-converted tap')
        self.assertEqual(macro('XVF_I2C_CAT_FAR_END_NATIVE'), 10,
                         'native-rate far end tap: 10 is the full-band one on a 48 kHz link')
        self.assertEqual(macro('XVF_I2C_CMD_OP_UPSAMPLE'), 14, 'AUDIO_MGR_OP_UPSAMPLE CmdID')
        self.assertEqual(macro('XVF_I2C_CMD_OP_L'), 15, 'AUDIO_MGR_OP_L CmdID')
        self.assertEqual(macro('XVF_I2C_CMD_OP_R'), 19, 'AUDIO_MGR_OP_R CmdID')
        self.assertEqual(macro('XVF_I2C_RESID_AUDIO_MGR'), 35, 'Audio Manager ResID')

    def mic_gain_mapper(self):
        lib = self.library()
        lib.wy_mic_gain_multiple.argtypes = [C.c_int]
        lib.wy_mic_gain_multiple.restype = C.c_float
        return lib

    def test_mic_gain_percentage_never_silences_the_microphone(self):
        """The fleet's `mic_gain` is a 0..100 percentage; the chip wants a multiplier.

        0 must NOT map to 0.0. A silenced microphone looks exactly like a hardware fault, which is
        the worst way for a setting to fail, so the bottom of the range is the quietest usable
        gain instead. The top of the range must be the documented maximum, and out-of-range values
        must clamp rather than extrapolate."""
        lib = self.mic_gain_mapper()
        self.assertAlmostEqual(lib.wy_mic_gain_multiple(0), 0.5, places=4,
                               msg='0% must not mute the microphone')
        self.assertAlmostEqual(lib.wy_mic_gain_multiple(-40), 0.5, places=4)
        # 78 is the fleet default and means unity: the level the microphone was tuned at.
        self.assertAlmostEqual(lib.wy_mic_gain_multiple(50), 50 / 78.0, places=4)
        self.assertAlmostEqual(lib.wy_mic_gain_multiple(78), 1.0, places=2,
                               msg='78 is the fleet default; the ceiling was raised after the '
                                   'bridge measured speech below its energy gate')
        self.assertAlmostEqual(lib.wy_mic_gain_multiple(100), 100 / 78.0, places=4)
        # Out of range clamps to the top of the PERCENT scale (100), not to the multiplier ceiling.
        self.assertAlmostEqual(lib.wy_mic_gain_multiple(250), 100 / 78.0, places=4,
                               msg='out of range must clamp, not extrapolate')

    def test_front_end_is_re_armed_on_every_boot(self):
        """AGC and mic gain are VOLATILE co-processor state: a co-processor restart wipes them.

        A co-processor restart happens on every power cycle and on every firmware write, so
        applying them once at provisioning would leave the device hard of hearing after the first
        reboot, with nothing in the log to say why. They must be armed from playback start."""
        src = (ROOT / 'firmware/esp32/main/audio_playback.c').read_text()
        start = src.index('int audio_playback_start(void)')
        self.assertIn('audio_xvf_apply_frontend(', src[start:],
                      'playback start must re-arm the front end on every boot')
        # The level must be armed on the tap we actually capture. Seeed routes both I2S output
        # channels to ASR beam 0, and XMOS does not feed that tap through the post-processor, so
        # the PP AGC cannot change this stream (measured: enabling it left the room at 73-75 where
        # it had been 75-77). The ASR tap and its own fixed gain are the controls that do.
        self.assertIn('XVF_I2C_CMD_ASR_OUT_ONOFF', src, 'the ASR output tap must be selected')
        self.assertIn('XVF_I2C_CMD_ASR_OUT_GAIN', src, 'the ASR fixed gain must be applied')
        self.assertNotIn('XVF_I2C_CMD_PP_AGCONOFF, 1', src,
                         'the PP AGC cannot affect the ASR tap and must not be presented as if it can')

    def test_mic_gain_config_reaches_the_chip(self):
        """`mic_gain` was validated, stored and reported to the fleet while never reaching the
        chip -- the device echoed a setting it had never applied. An adapter has to exist, and it
        has to call the real setter."""
        src = (ROOT / 'firmware/esp32/main/gateway_task.c').read_text()
        self.assertIn('audio_xvf_set_mic_gain', src,
                      'the config apply path must actually write the gain to the co-processor')
        self.assertIn('"mic_gain"', src)

    def test_playback_batch_matches_the_tx_dma_descriptor(self):
        """A write SMALLER than one TX DMA descriptor gets paced by the descriptor cycle rather
        than by the audio clock: the audio still plays, but slower than realtime, and no counter
        records a stall or a drop because nothing was lost.

        Measured with the old 128-frame batch (sized for 16 kHz, where it is 8 ms, but only
        2.67 ms at 48 kHz): a local 3.000 s tone took 6.10 s to drain -- 2.03x slow with
        0 stalled / 0 abandoned. The batch and dma_frame_num must therefore agree, and the batch
        must be a sensible duration at 48 kHz."""
        src = (ROOT / 'firmware/esp32/main/audio_playback.c').read_text()
        batch_int32 = int(re.search(r'#define PLAYBACK_FRAME_BATCH\s+(\d+)', src).group(1))
        frames = batch_int32 // 2          # two int32 per stereo frame
        dma = int(re.search(r'channel\.dma_frame_num\s*=\s*(\d+)', src).group(1))
        self.assertEqual(frames, dma,
                         'the write batch (%d frames) must equal one TX DMA descriptor (%d)'
                         % (frames, dma))
        millis = frames / 48000.0 * 1000.0
        self.assertGreaterEqual(millis, 8.0,
                                'a %.1f ms batch is too short at 48 kHz' % millis)

    def test_playback_reports_underrun_counters(self):
        """Stutter has several indistinguishable causes by ear: a dry ring, a DMA that refuses
        writes, or a batch cut short. The pump has to report which, once per answer, or every
        investigation becomes guesswork. The counters must exist and must be reset per answer,
        otherwise a number from a previous answer is read as this one's."""
        src = (ROOT / 'firmware/esp32/main/audio_playback.c').read_text()
        for counter in ('pump_empty', 'pump_stalled', 'pump_abandoned', 'pump_writes'):
            self.assertIn(counter, src, '%s counter missing' % counter)
        self.assertIn('playback stats', src, 'the summary line must be logged')
        # An empty ring only counts as starvation while a stream is ACTIVE; after the stream ends
        # the ring draining is normal and would otherwise report a fault on every clean answer.
        self.assertIn('wy_session_playback_active(s)) pump_empty++', src,
                      'empty-ring must only count during an active stream')
        self.assertIn('pump_writes = pump_empty = pump_stalled = pump_abandoned = 0', src,
                      'counters must reset at the start of each answer')

    def test_i2s_dma_depth_covers_a_scheduling_hiccup(self):
        """At 48 kHz stereo 32-bit one frame is 8 bytes, so the SDK's default DMA (6 x 240 frames)
        is only about 11 ms of slack -- less than a single WiFi or TLS burst.

        The measured symptom this guards: microphone accounting read exactly 16000 samples/s idle
        and 15,200-15,920 whenever playback ran, returning to 16000 when it stopped. A dropped
        microphone sample is unrecoverable (the DMA has already overwritten it), so both
        directions have to carry real depth rather than the default."""
        for name, fname in (('capture', 'audio_capture.c'), ('playback', 'audio_playback.c')):
            src = (ROOT / 'firmware/esp32/main' / fname).read_text()
            desc = int(re.search(r'channel\.dma_desc_num\s*=\s*(\d+)', src).group(1))
            frames = int(re.search(r'channel\.dma_frame_num\s*=\s*(\d+)', src).group(1))
            millis = desc * frames * 8 / 48000.0 * 1000.0
            self.assertGreaterEqual(millis, 30.0,
                                    '%s DMA is only %.0f ms at 48 kHz; a scheduling hiccup will '
                                    'overwrite audio' % (name, millis))

    def test_tasks_are_partitioned_by_core_not_just_by_priority(self):
        """Core placement is the contract, because priority alone cannot protect audio.

        The ESP32-S3 has two cores and the radio/TLS stack runs at a priority no application task
        can outrank. So the guarantee is spatial: capture and the playback pump own core 1 alone,
        while WiFi/lwIP/TLS, the gateway task and the LED worker share core 0. Cross-core priority
        comparisons are meaningless; WITHIN each core there is still an order that matters.

        Within the audio core: capture outranks playback, because a dropped microphone sample is
        unrecoverable while a late playback write only costs latency. Within the UI/network core:
        the LED must not be starved by a TLS burst, or the wake feedback lags the wake."""
        def source(fname):
            return (ROOT / 'firmware/esp32/main' / fname).read_text()

        def priority(fname, macro):
            return int(re.search(r'#define %s\s+(\d+)' % macro, source(fname)).group(1))

        def core_of(fname, fn):
            # the core id is the last argument of xTaskCreatePinnedToCore(...)
            src = source(fname)
            m = re.search(r'xTaskCreatePinnedToCore\(\s*' + fn + r'.[^;]*?,\s*(\d)\s*\)', src,
                          re.S)
            self.assertIsNotNone(m, '%s must be pinned to a core explicitly' % fn)
            return int(m.group(1))

        self.assertEqual(core_of('audio_capture.c', 'capture_task_fn'), 1,
                         'capture must own the audio core')
        # The pump must NOT share the capture core: capture runs wake-model inference every 10 ms at
        # priority 6, and a pump below it there starved playback to ~7% of realtime.
        self.assertEqual(core_of('audio_playback.c', 'playback_task_fn'), 0,
                         'the pump must not share the inference core')
        self.assertGreaterEqual(priority('audio_playback.c', 'PLAYBACK_TASK_PRIORITY'),
                                priority('gateway_task.c', 'GATEWAY_TASK_PRIORITY'),
                                'the pump must not be starved by TLS work on its core')
        self.assertEqual(core_of('gateway_task.c', 'gateway_task_fn'), 0,
                         'the TLS reader must stay off the audio core')
        self.assertEqual(core_of('led_status.c', 'led_worker'), 0,
                         'the LED worker must stay off the audio core')

        # Capture and the pump are on DIFFERENT cores, so their priorities are not comparable;
        # what matters is that neither shares a core with the other's workload.
        self.assertGreaterEqual(priority('led_status.c', 'LED_WORKER_PRIORITY'),
                                priority('gateway_task.c', 'GATEWAY_TASK_PRIORITY'),
                                'wake feedback must not be starved by network work')

    def test_playback_leaves_the_output_mux_at_the_image_defaults(self):
        """Playback must NOT clear the upsampling flags or select the category-10 far end.

        This test previously DEMANDED both, on the reading that they are what carry the 48 kHz
        profile to the speaker. They are in fact the XMOS guide's host-to-host LOOPBACK test ("to
        loop back any data received"), and category 10 is far-end audio rather than a microphone
        tap. While the scalar count byte was wrong these writes were inert, so the mistake was
        invisible; once writes began to apply, the captured voice collapsed to a median RMS of 16
        because the output carried the silent far end instead of the microphones.

        The premise was also weaker than it looked: the voice pipeline is 16 kHz internally and a
        48 kHz interface adds a sample-rate converter, so "full band" never applied to the
        microphone, whose documented range is 80 Hz-8 kHz either way.
        """
        src = (ROOT / 'firmware/esp32/main/audio_playback.c').read_text()
        start = src.index('int audio_playback_start(void)')
        body = src[start:]
        code = '\n'.join(line for line in body.splitlines()
                         if not line.lstrip().startswith(('*', '/*', '//')))
        self.assertNotIn('audio_xvf_output_upsample(0, 0)', code,
                         'clearing the upsampling flags is the LOOPBACK test, not a playback setup')
        self.assertNotIn('audio_xvf_route_output(', code,
                         'the output mux must stay at the image defaults; category 10 is far-end '
                         'audio and silences the microphone capture')

    def test_the_coprocessor_owns_the_clock_in_every_i2s_direction(self):
        """The link rate belongs to the co-processor, not to us. Upstream's I2S variants are either
        a 48 kHz MASTER (it drives BCLK/WS) or a 16 kHz SLAVE (the host drives them), and the
        readme is explicit that "the two devices must use complementary roles. Connecting two
        masters causes clock contention, while connecting two slaves leaves the bus without a clock
        source."

        With the co-processor holding a 48 kHz master image, a MASTER role on ANY ESP32 direction
        means two chips driving the same BCLK/WS lines: the failure is not silence, it is contention
        on shared pins, and it can present as corrupted audio rather than an obvious error. All three
        channel creations (capture RX, playback TX, and the bring-up probe) must agree, because the
        SDK pairs RX and TX into one full-duplex controller and only demotes a master when BOTH
        directions are master -- so a lone MASTER half is exactly the case the driver will not fix.
        """
        files = {
            'capture RX': ROOT / 'firmware/esp32/main/audio_capture.c',
            'playback TX': ROOT / 'firmware/esp32/main/audio_playback.c',
            'bring-up probe': ROOT / 'firmware/esp32/main/audio_bringup.c',
        }
        for label, path in files.items():
            src = path.read_text()
            masters = [ln.strip() for ln in src.splitlines()
                       if 'I2S_CHANNEL_DEFAULT_CONFIG' in ln and 'I2S_ROLE_MASTER' in ln]
            self.assertEqual(masters, [],
                             f'{label}: I2S_CHANNEL_DEFAULT_CONFIG(..., I2S_ROLE_MASTER) puts a '
                             f'clock driver on lines the co-processor is already driving: {masters}')
            chan_lines = [ln for ln in src.splitlines() if 'I2S_CHANNEL_DEFAULT_CONFIG' in ln]
            self.assertTrue(chan_lines, f'{label}: no channel creation found, test shape is stale')
            for ln in chan_lines:
                self.assertIn('_ROLE', ln,
                              f'{label}: channel role must come from the named ROLE constant so it '
                              f'is greppable and reviewable: {ln.strip()}')
        # the role constants themselves, pinned explicitly
        for label, name, path in (
                ('capture RX', 'CAPTURE_I2S_ROLE', files['capture RX']),
                ('playback TX', 'PLAYBACK_I2S_ROLE', files['playback TX']),
                ('bring-up probe', 'XVF_I2S_ROLE_PROBE', files['bring-up probe'])):
            m = re.search(rf'^\s*#define\s+{name}\s+(\w+)', path.read_text(), re.M)
            if m is None:
                self.fail(f'{name} is not defined in {label}')
            self.assertEqual(m.group(1), 'I2S_ROLE_SLAVE',
                             f'{label} must be a clock SLAVE: the co-processor is the master')


if __name__ == '__main__':
    unittest.main()
