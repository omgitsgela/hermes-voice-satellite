#!/usr/bin/env python3
"""#110: the microWakeWord inference policy (wy_mww) and the shipped model's numbers.

The device scorer is two halves: a portable policy module (wy_mww.c -- the
manifest parameters, the feature preprocessor settings, the int8 feature
scaling, the streaming stride bookkeeping, and the sliding-window /
probability-cutoff decision) and the ESP/TFLite-Micro driver around it
(wy_mww_scorer.cpp). This test compiles the portable half on the host with
-std=c11 -Wall -Wextra -Werror and pins every number that must agree between
four places that cannot see each other at runtime:

  1. firmware/esp32/models/hey_hermes.json -- the manifest of the bundled
     default model (probability_cutoff, sliding_window_size, feature_step_size,
     tensor_arena_size). The device cannot read it (the model partition
     carries the bare .tflite under the WKM1 header), so the firmware's
     wy_mww_params_default() must equal it; this test fails if they drift. A
     provisioned model also carries these numbers in its own v2 blob header,
     so the defaults are the fallback, not the only path.
  2. firmware/esp32/models/hey_hermes.tflite -- the model tensor layout the
     scorer validates at init ([1, stride, 40] int8 in, [1, 1] uint8 out),
     read here with a minimal flatbuffer walker so the feature dimensions are
     grounded in the shipped file rather than assumed.
  3. wy_mww_frontend_params() -- the fixed microWakeWord preprocessor settings
     every model trains against (ESPHome's preprocessor_settings.h).
  4. wy_mww_feature_quantize() -- the int8 feature scaling the quantized model
     expects.

Plus the detection decision itself: the window/cutoff comparison (strictly
above int(cutoff * 255) * window_size), the one-shot crossing, and the stride
cadence that decides when the streaming interpreter is invoked.

Run:  ./.venv/bin/python -B scripts/test_esp32_wake_scorer.py
"""
import ctypes as C
import hashlib
import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / 'firmware/esp32/components'
MWW = COMP / 'wy_mww'
MODELS = ROOT / 'firmware/esp32/models'

# The delivered SHA-256 of the bundled model, as recorded in models/NOTICE.md.
NOTICE_TFLITE_SHA256 = '377545239b78e0671a92f1ae7a4bbbf9a18c170263edb9aba3db5f23fcb0ed4e'

# TensorType bytes of the tflite flatbuffer schema (schema_generated.h).
TENSOR_TYPE_UINT8 = 3
TENSOR_TYPE_INT8 = 9


def parse_tflite_io(path):
    """Minimal flatbuffer reader: the model's input/output tensor layout.

    Returns {'version', 'inputs': [tensor...], 'outputs': [tensor...]} where a
    tensor is {'name', 'shape', 'type', 'quant': (scale, zero_point)}. Only the
    schema's table/vector walk is implemented -- no external dependencies, so
    the suite stays offline.
    """
    b = path.read_bytes()

    def u16(pos):
        return struct.unpack_from('<H', b, pos)[0]

    def i32(pos):
        return struct.unpack_from('<i', b, pos)[0]

    def u32(pos):
        return struct.unpack_from('<I', b, pos)[0]

    def deref(pos):
        return pos + u32(pos)

    def table_at(pos):
        vt = pos - i32(pos)
        vt_size = u16(vt)

        def field(fid):
            vo = 4 + 2 * fid
            if vo + 2 > vt_size:
                return None
            off = u16(vt + vo)
            return None if off == 0 else pos + off

        return field

    def vec_offsets(fpos):
        vpos = deref(fpos)
        return [deref(vpos + 4 + 4 * i) for i in range(u32(vpos))]

    def vec_scalars(fpos, code):
        vpos = deref(fpos)
        count = u32(vpos)
        return list(struct.unpack_from('<%d%s' % (count, code), b, vpos + 4))

    def string_at(fpos):
        pos = deref(fpos)
        length = u32(pos)
        return b[pos + 4:pos + 4 + length].decode()

    def tensor_at(pos):
        t = table_at(pos)
        qpos = t(4)
        quant = None
        if qpos is not None:
            q = table_at(deref(qpos))  # table fields are slot-relative uoffsets
            scale = zero = None
            if q(2) is not None:
                scale = vec_scalars(q(2), 'f')[0]
            if q(3) is not None:
                zero = vec_scalars(q(3), 'q')[0]
            quant = (scale, zero)
        return {
            'name': string_at(t(3)) if t(3) is not None else '',
            'shape': vec_scalars(t(0), 'i'),
            'type': b[t(1)],
            'quant': quant,
        }

    model = table_at(u32(0))
    subgraph = table_at(vec_offsets(model(2))[0])
    tensors = vec_offsets(subgraph(0))
    inputs = vec_scalars(subgraph(1), 'i')
    outputs = vec_scalars(subgraph(2), 'i')
    return {
        'version': u32(model(0)),
        'inputs': [tensor_at(tensors[i]) for i in inputs],
        'outputs': [tensor_at(tensors[i]) for i in outputs],
    }


class WyMwwLibrary:
    """The portable policy compiled on the host, exactly as the firmware has it."""

    def __init__(self):
        source = MWW / 'wy_mww.c'
        assert source.exists(), 'missing %s' % source
        self._temporary = tempfile.TemporaryDirectory()
        self.path = Path(self._temporary.name) / 'wy_mww.so'
        subprocess.run(
            ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
             '-I' + str(MWW), str(source), '-o', str(self.path)], check=True)
        self.lib = C.CDLL(str(self.path))

        class Params(C.Structure):
            _fields_ = [('probability_cutoff', C.c_float),
                        ('sliding_window_size', C.c_uint),
                        ('feature_step_ms', C.c_uint),
                        ('tensor_arena_size', C.c_uint),
                        ('feature_size', C.c_uint)]

        class Frontend(C.Structure):
            _fields_ = [('window_size_ms', C.c_uint),
                        ('step_size_ms', C.c_uint),
                        ('num_channels', C.c_uint),
                        ('lower_band_limit', C.c_float),
                        ('upper_band_limit', C.c_float),
                        ('smoothing_bits', C.c_uint),
                        ('even_smoothing', C.c_float),
                        ('odd_smoothing', C.c_float),
                        ('min_signal_remaining', C.c_float),
                        ('enable_pcan', C.c_int),
                        ('pcan_strength', C.c_float),
                        ('pcan_offset', C.c_float),
                        ('pcan_gain_bits', C.c_uint),
                        ('enable_log', C.c_int),
                        ('log_scale_shift', C.c_uint)]

        class Stream(C.Structure):
            _fields_ = [('cutoff_q', C.c_uint),
                        ('window_size', C.c_uint),
                        ('detect_sum', C.c_uint),
                        ('stride', C.c_uint),
                        ('slice', C.c_uint),
                        ('head', C.c_uint),
                        ('sum', C.c_ulonglong),
                        ('probs', C.c_uint8 * 32)]

        self.Params = Params
        self.Frontend = Frontend
        self.Stream = Stream

        lib = self.lib
        lib.wy_mww_params_default.argtypes = [C.POINTER(Params)]
        lib.wy_mww_params_parse.argtypes = [C.c_char_p, C.c_size_t, C.POINTER(Params)]
        lib.wy_mww_params_parse.restype = C.c_int
        lib.wy_mww_cutoff_quantized.argtypes = [C.c_float]
        lib.wy_mww_cutoff_quantized.restype = C.c_uint
        lib.wy_mww_frontend_params.argtypes = [C.POINTER(Frontend), C.c_uint]
        lib.wy_mww_feature_quantize.argtypes = [C.c_uint16]
        lib.wy_mww_feature_quantize.restype = C.c_int
        lib.wy_mww_stream_init.argtypes = [C.POINTER(Stream), C.POINTER(Params), C.c_uint]
        lib.wy_mww_stream_init.restype = C.c_int
        lib.wy_mww_stream_reset.argtypes = [C.POINTER(Stream)]
        lib.wy_mww_stream_slot.argtypes = [C.POINTER(Stream)]
        lib.wy_mww_stream_slot.restype = C.c_uint
        lib.wy_mww_stream_advance.argtypes = [C.POINTER(Stream)]
        lib.wy_mww_stream_advance.restype = C.c_int
        lib.wy_mww_stream_push.argtypes = [C.POINTER(Stream), C.c_uint8]
        lib.wy_mww_stream_push.restype = C.c_float

    def close(self):
        self._temporary.cleanup()

    def defaults(self):
        params = self.Params()
        self.lib.wy_mww_params_default(C.byref(params))
        return params

    def parse(self, text):
        params = self.Params()
        rc = self.lib.wy_mww_params_parse(text.encode(), len(text), C.byref(params))
        return rc, params

    def stream(self, cutoff=0.5, window=5, stride=3):
        params = self.Params(cutoff, window, 10, 32000, 40)
        stream = self.Stream()
        assert self.lib.wy_mww_stream_init(C.byref(stream), C.byref(params), stride) == 0
        return stream


class WakeScorerPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mww = WyMwwLibrary()

    @classmethod
    def tearDownClass(cls):
        cls.mww.close()

    # ---- the manifest: firmware defaults vs the shipped JSON -----------------

    def test_defaults_are_the_shipped_manifest(self):
        """wy_mww_params_default() is the fallback policy (a model provisioned
        without its manifest), so it must equal the bundled default model's
        models/hey_hermes.json exactly."""
        manifest = json.loads((MODELS / 'hey_hermes.json').read_text())
        micro = manifest['micro']
        self.assertEqual(manifest['version'], 2, 'microWakeWord v2 streaming model')
        defaults = self.mww.defaults()
        self.assertAlmostEqual(defaults.probability_cutoff,
                               micro['probability_cutoff'], places=6)
        self.assertEqual(defaults.sliding_window_size, micro['sliding_window_size'])
        self.assertEqual(defaults.feature_step_ms, micro['feature_step_size'])
        self.assertEqual(defaults.tensor_arena_size, micro['tensor_arena_size'])
        self.assertEqual(defaults.feature_size, 40)

    def test_manifest_parser_reads_the_real_file(self):
        text = (MODELS / 'hey_hermes.json').read_text()
        rc, params = self.mww.parse(text)
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(params.probability_cutoff, 0.5, places=6)
        self.assertEqual(params.sliding_window_size, 5)
        self.assertEqual(params.feature_step_ms, 10)
        self.assertEqual(params.tensor_arena_size, 32000)

    def test_manifest_parser_tolerates_layout_but_not_nonsense(self):
        reordered = ('{"micro": {"tensor_arena_size": 4096, "feature_step_size": 20,\n'
                     '  "sliding_window_size": 3, "probability_cutoff": 0.9}}')
        rc, params = self.mww.parse(reordered)
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(params.probability_cutoff, 0.9, places=6)
        self.assertEqual(params.sliding_window_size, 3)
        self.assertEqual(params.feature_step_ms, 20)
        self.assertEqual(params.tensor_arena_size, 4096)
        for bad in ('{}',
                    '{"micro": {"probability_cutoff": 0.5, "sliding_window_size": 5,'
                    ' "feature_step_size": 10}}',                       # missing key
                    '{"micro": {"probability_cutoff": 0, "sliding_window_size": 5,'
                    ' "feature_step_size": 10, "tensor_arena_size": 32000}}',   # no cutoff
                    '{"micro": {"probability_cutoff": 1.5, "sliding_window_size": 5,'
                    ' "feature_step_size": 10, "tensor_arena_size": 32000}}',   # out of range
                    '{"micro": {"probability_cutoff": 0.5, "sliding_window_size": 33,'
                    ' "feature_step_size": 10, "tensor_arena_size": 32000}}',   # window too big
                    '{"micro": {"probability_cutoff": 0.5, "sliding_window_size": 5,'
                    ' "feature_step_size": 10.5, "tensor_arena_size": 32000}}', # not whole
                    '{"micro": {"probability_cutoff": 0.5, "sliding_window_size": 5,'
                    ' "feature_step_size": 10, "tensor_arena_size": 100}}'):    # arena too small
            rc, _ = self.mww.parse(bad)
            self.assertEqual(rc, -1, 'must reject: %s' % bad)

    # ---- the model file: tensor layout + identity ---------------------------

    def test_model_tensor_layout_matches_the_pipeline(self):
        """The scorer validates this layout on-device; the numbers are pinned
        here against the shipped file so '40 features, stride from the tensor'
        is grounded, not assumed."""
        info = parse_tflite_io(MODELS / 'hey_hermes.tflite')
        self.assertEqual(info['version'], 3, 'tflite schema version')
        self.assertEqual(len(info['inputs']), 1)
        self.assertEqual(len(info['outputs']), 1)
        inp, out = info['inputs'][0], info['outputs'][0]
        self.assertEqual(inp['shape'], [1, 3, 40], 'input [1, stride, features]')
        self.assertEqual(inp['type'], TENSOR_TYPE_INT8)
        self.assertEqual(out['shape'], [1, 1])
        self.assertEqual(out['type'], TENSOR_TYPE_UINT8)
        # Quantization: input scale/zero per the log-mel int8 mapping, output
        # probability on the 0..255 scale (scale 1/256, zero 0).
        self.assertAlmostEqual(inp['quant'][0], 0.10196078568696976, places=6)
        self.assertEqual(inp['quant'][1], -128)
        self.assertAlmostEqual(out['quant'][0], 0.00390625, places=9)
        self.assertEqual(out['quant'][1], 0)
        # The policy's feature size is the tensor's dim 2; the stride (dim 1)
        # is read from the tensor at init and must fit the fixed bookkeeping.
        self.assertEqual(self.mww.defaults().feature_size, inp['shape'][2])
        self.assertTrue(1 <= inp['shape'][1] <= 32)

    def test_model_bytes_are_the_notice_bytes(self):
        digest = hashlib.sha256((MODELS / 'hey_hermes.tflite').read_bytes()).hexdigest()
        self.assertEqual(digest, NOTICE_TFLITE_SHA256,
                         'the bundled model changed without a NOTICE.md update')

    # ---- the fixed preprocessor settings ------------------------------------

    def test_frontend_params_are_the_training_settings(self):
        f = self.mww.Frontend()
        self.mww.lib.wy_mww_frontend_params(C.byref(f), 10)
        self.assertEqual(f.window_size_ms, 30)
        self.assertEqual(f.step_size_ms, 10, 'the manifest step size flows through')
        self.assertEqual(f.num_channels, 40)
        self.assertAlmostEqual(f.lower_band_limit, 125.0, places=3)
        self.assertAlmostEqual(f.upper_band_limit, 7500.0, places=3)
        self.assertEqual(f.smoothing_bits, 10)
        self.assertAlmostEqual(f.even_smoothing, 0.025, places=5)
        self.assertAlmostEqual(f.odd_smoothing, 0.06, places=5)
        self.assertAlmostEqual(f.min_signal_remaining, 0.05, places=5)
        self.assertEqual(f.enable_pcan, 1)
        self.assertAlmostEqual(f.pcan_strength, 0.95, places=5)
        self.assertAlmostEqual(f.pcan_offset, 80.0, places=3)
        self.assertEqual(f.pcan_gain_bits, 21)
        self.assertEqual(f.enable_log, 1)
        self.assertEqual(f.log_scale_shift, 6)

    # ---- the int8 feature scaling ------------------------------------------

    def test_feature_quantize_matches_the_quantized_frontend(self):
        """input = clamp(((feature * 256) + 333) / 666 - 128)."""
        q = self.mww.lib.wy_mww_feature_quantize
        cases = [(0, -128), (1, -128), (132, -77), (330, -1), (332, 0),
                 (335, 1), (670, 127), (65535, 127)]
        for feature, expected in cases:
            self.assertEqual(q(feature), expected, 'feature %d' % feature)

    # ---- the cutoff quantization -------------------------------------------

    def test_cutoff_quantization_truncates_like_the_reference(self):
        q = self.mww.lib.wy_mww_cutoff_quantized
        self.assertEqual(q(0.5), 127, 'int(0.5 * 255) truncates to 127')
        self.assertEqual(q(0.97), 247)
        self.assertEqual(q(1.0), 255)
        self.assertEqual(q(0.05), 12)

    # ---- the streaming bookkeeping + detection decision ---------------------

    def test_stride_cadence_invokes_every_stride_slices(self):
        s = self.mww.stream(stride=3)
        lib = self.mww.lib
        slots, invokes = [], []
        for _ in range(6):
            slots.append(lib.wy_mww_stream_slot(C.byref(s)))
            invokes.append(lib.wy_mww_stream_advance(C.byref(s)))
        self.assertEqual(slots, [0, 1, 2, 0, 1, 2])
        self.assertEqual(invokes, [0, 0, 1, 0, 0, 1])

    def test_window_needs_a_sustained_mean_above_the_cutoff(self):
        s = self.mww.stream(cutoff=0.5, window=5)  # cutoff_q = 127
        lib = self.mww.lib
        # Exactly at the cutoff: sum == 127 * 5 == 635 is NOT a detection
        # (the reference compares strictly above).
        for _ in range(5):
            self.assertEqual(lib.wy_mww_stream_push(C.byref(s), 127), 0.0)
        # One count above tips the window: 636 > 635. The score is the window
        # mean on the 0..1 scale (636 / (5 * 255)).
        score = lib.wy_mww_stream_push(C.byref(s), 128)
        self.assertAlmostEqual(score, 636.0 / 1275.0, places=5)

    def test_bundled_cutoff_is_the_model_own_gate(self):
        """The bundled default ships cutoff 0.5 (cutoff_q 127), the value in
        its own manifest, and the firmware defaults must agree with it. The
        cutoff is the model's primary selectivity gate: the scorer reports a
        score only when the 5-slice window mean crosses it. A window mean at
        or below the cutoff must NOT fire; a sustained match must."""
        d = self.mww.defaults()
        self.assertAlmostEqual(d.probability_cutoff, 0.5, places=6)
        s = self.mww.stream(cutoff=0.5, window=5)  # cutoff_q = 127
        lib = self.mww.lib
        # A weak match: five slices at quantized 0.4 (102) sum to 510, under
        # 127 * 5 == 635, so no detection.
        for _ in range(5):
            self.assertEqual(lib.wy_mww_stream_push(C.byref(s), 102), 0.0)
        # A sustained match at the cutoff: 5 * 128 == 640 > 635 crosses on the
        # fifth high, and the score is the window mean (640 / 1275).
        s2 = self.mww.stream(cutoff=0.5, window=5)
        for _ in range(4):
            self.assertEqual(lib.wy_mww_stream_push(C.byref(s2), 128), 0.0)
        self.assertAlmostEqual(lib.wy_mww_stream_push(C.byref(s2), 128),
                               640.0 / 1275.0, places=6)

    def test_detection_is_one_shot_and_the_window_refills(self):
        s = self.mww.stream(cutoff=0.5, window=5)
        lib = self.mww.lib
        # Sustained max probabilities cross as soon as the window mean passes
        # the cutoff: 3 * 255 = 765 > 635 on the third push (the window is
        # zero-initialised, so the mean must climb).
        self.assertEqual(lib.wy_mww_stream_push(C.byref(s), 255), 0.0)
        self.assertEqual(lib.wy_mww_stream_push(C.byref(s), 255), 0.0)
        self.assertAlmostEqual(lib.wy_mww_stream_push(C.byref(s), 255),
                               765.0 / 1275.0, places=5)
        # The crossing consumed the window, so the fire is one-shot: the window
        # must accumulate again before the next crossing (same three pushes).
        self.assertEqual(lib.wy_mww_stream_push(C.byref(s), 255), 0.0)
        self.assertEqual(lib.wy_mww_stream_push(C.byref(s), 255), 0.0)
        self.assertAlmostEqual(lib.wy_mww_stream_push(C.byref(s), 255),
                               765.0 / 1275.0, places=5)

    def test_spikes_average_out_below_the_cutoff(self):
        s = self.mww.stream(cutoff=0.5, window=5)
        lib = self.mww.lib
        # Alternating 200/silence: any five-slice window holds three 200s
        # (sum 600 <= 635), so the sustained-mean requirement holds it off.
        for i in range(20):
            self.assertEqual(lib.wy_mww_stream_push(C.byref(s), 200 if i % 2 == 0 else 0), 0.0)

    def test_stricter_window_size_needs_more_slices(self):
        s = self.mww.stream(cutoff=0.9, window=8)  # cutoff_q = 229
        lib = self.mww.lib
        for _ in range(7):
            self.assertEqual(lib.wy_mww_stream_push(C.byref(s), 250), 0.0)
        # 7 * 250 = 1750 <= 229 * 8 = 1832; the 8th slice tips it.
        self.assertGreater(lib.wy_mww_stream_push(C.byref(s), 250), 0.9)

    def test_reset_zeroes_window_and_stride(self):
        s = self.mww.stream(stride=3)
        lib = self.mww.lib
        lib.wy_mww_stream_push(C.byref(s), 255)
        lib.wy_mww_stream_advance(C.byref(s))
        lib.wy_mww_stream_advance(C.byref(s))
        self.assertEqual(lib.wy_mww_stream_slot(C.byref(s)), 2)
        lib.wy_mww_stream_reset(C.byref(s))
        self.assertEqual(lib.wy_mww_stream_slot(C.byref(s)), 0)
        self.assertEqual(s.sum, 0)
        # A cold window does not detect on a single high slice.
        self.assertEqual(lib.wy_mww_stream_push(C.byref(s), 255), 0.0)

    def test_stream_init_rejects_out_of_bounds(self):
        lib = self.mww.lib
        params = self.mww.Params(0.5, 5, 10, 32000, 40)
        stream = self.mww.Stream()
        self.assertEqual(lib.wy_mww_stream_init(C.byref(stream), C.byref(params), 0), -1)
        self.assertEqual(lib.wy_mww_stream_init(C.byref(stream), C.byref(params), 33), -1)
        bad = self.mww.Params(0.5, 33, 10, 32000, 40)
        self.assertEqual(lib.wy_mww_stream_init(C.byref(stream), C.byref(bad), 3), -1)
        wrong_features = self.mww.Params(0.5, 5, 10, 32000, 32)
        self.assertEqual(lib.wy_mww_stream_init(C.byref(stream), C.byref(wrong_features), 3), -1)
        self.assertEqual(lib.wy_mww_stream_init(C.byref(stream), None, 3), -1)


if __name__ == '__main__':
    unittest.main()
