#!/usr/bin/env python3
"""Compile the production wake detector core; exercise its decision logic.

Host half of increment 4 (the local wake detector). The core (wy_wake) is a
pure C state machine around one question per frame of microphone audio: does
the frame's score cross the configured wake threshold, and is the device in a
state where a detection is allowed (not streaming, not playing, not cooling
down)? Every input is injected -- a scorer (#110 supplies the real one; the
tests use a scripted stub), a model loader, a chime, and a wake trigger (the
device glue's wy_session_wake) -- so the production source compiles on the
host with -Werror and is tested directly.

The scripted stub scorer (test_esp32_wake_driver.c) returns one score per feed
call from a file-static array, so a test is deterministic and never fires on
an unscripted frame. The driver also records the chime/wake call order (the
chime-then-wake property) and the model name the load seam was asked for.

Tasks (per .hermes/plans/2026-10-06_133000-esp32-increment-4-wake-detector.md):
  1. threshold decision        2. deaf guard (streaming + playback + cooldown)
  3. chime-then-wake ordering  4. threshold + model from config
"""
import ctypes as C
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / 'firmware/esp32/components'
DRIVER = Path(__file__).resolve().parent / 'test_esp32_wake_driver.c'


class WakeCoreTests(unittest.TestCase):
    def setUp(self):
        self._keepalive = []

    def library(self):
        """Compile the production core plus the test driver into one host .so."""
        source = COMP / 'wy_wake/wy_wake.c'
        self.assertTrue(source.exists(), 'wake core missing: %s' % source)
        self.assertTrue(DRIVER.exists(), 'test driver missing: %s' % DRIVER)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'wake.so'
        subprocess.run(
            ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
             '-I' + str(COMP / 'wy_wake'),
             str(source), str(DRIVER), '-lm', '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        # Test-driver API.
        # NOTE: in this environment ctypes caches attribute access (lib.name) and
        # string indexing (lib[name]) as SEPARATE _FuncPtr objects, so a
        # restype set on one is invisible to calls through the other. Every
        # restype/argtype below is therefore set via getattr (attribute
        # identity), matching the call sites, which all use lib.name.
        lib.td_reset.argtypes = [C.POINTER(C.c_float), C.c_int]
        lib.td_reset.restype = None
        for name in ('td_get_score_calls', 'td_get_chime_calls', 'td_get_wake_calls',
                     'td_chime_then_wake', 'td_get_load_calls'):
            getattr(lib, name).restype = C.c_int
        lib.td_loaded_name_str.restype = C.c_char_p
        lib.td_set_load_fail.argtypes = [C.c_int]
        lib.td_set_wake_result.argtypes = [C.c_int]
        for name in ('td_addr_score', 'td_addr_load', 'td_addr_chime', 'td_addr_wake'):
            # These return 64-bit function addresses: with the default c_int
            # restype the upper 32 bits are silently dropped, so the core's
            # seams would point at garbage. c_void_p is mandatory.
            getattr(lib, name).restype = C.c_void_p
        # Production API.
        lib.wy_wake_size.restype = C.c_size_t
        lib.wy_wake_set_seams.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p,
                                          C.c_void_p, C.c_void_p, C.c_void_p]
        lib.wy_wake_set_seams.restype = None
        lib.wy_wake_init.argtypes = [C.c_void_p, C.c_double, C.c_uint, C.c_char_p]
        lib.wy_wake_init.restype = C.c_int
        lib.wy_wake_feed.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t]
        lib.wy_wake_feed.restype = C.c_int
        lib.wy_wake_set_streaming.argtypes = [C.c_void_p, C.c_int]
        lib.wy_wake_set_playback_active.argtypes = [C.c_void_p, C.c_int]
        lib.wy_wake_stream_ended.argtypes = [C.c_void_p]
        lib.wy_wake_deaf.argtypes = [C.c_void_p]
        lib.wy_wake_deaf.restype = C.c_bool
        lib.wy_wake_has_model.argtypes = [C.c_void_p]
        lib.wy_wake_has_model.restype = C.c_bool
        # D4 chime render (pure math).
        lib.wy_chime_render.argtypes = [C.c_void_p, C.c_size_t]
        lib.wy_chime_render.restype = C.c_size_t
        return lib

    def detector(self, lib, scores, threshold=0.35, cooldown_ms=0,
                 model_name=b'hey_hermes', with_load=True):
        """A fresh, fully-wired core: the driver's scripted scorer, load, chime
        and wake seams (the driver records every call)."""
        buf = C.create_string_buffer(lib.wy_wake_size())
        self._keepalive.append(buf)
        if scores:
            array = (C.c_float * len(scores))(*scores)
            lib.td_reset(array, len(scores))
        else:
            lib.td_reset(None, 0)
        load = lib.td_addr_load() if with_load else None
        lib.wy_wake_set_seams(buf, lib.td_addr_score(), load,
                              lib.td_addr_chime(), lib.td_addr_wake(), None)
        self.assertEqual(lib.wy_wake_init(buf, threshold, cooldown_ms, model_name), 0)
        return buf

    def feed(self, lib, det, frames=160):
        """One 10 ms chunk (160 mono16 samples) through the core."""
        pcm = C.create_string_buffer(frames * 2)
        return lib.wy_wake_feed(det, pcm, frames)

    # -- task 1: threshold decision -------------------------------------------
    def test_sub_threshold_frame_does_not_fire(self):
        """A frame whose score is below the configured threshold is consumed
        without a detection: no chime, no wake, the core stays armed (not
        deaf)."""
        lib = self.library()
        det = self.detector(lib, [0.10, 0.20, 0.34], threshold=0.35)
        for _ in range(3):
            self.assertEqual(self.feed(lib, det), 0)
        self.assertEqual(lib.td_get_chime_calls(), 0)
        self.assertEqual(lib.td_get_wake_calls(), 0)
        self.assertFalse(lib.wy_wake_deaf(det))

    def test_at_threshold_fires(self):
        """A frame whose score is EXACTLY the threshold fires: the bridge's
        semantics are >=, not >, so 0.35 at threshold 0.35 is a detection."""
        lib = self.library()
        det = self.detector(lib, [0.35], threshold=0.35)
        self.feed(lib, det)
        self.assertEqual(lib.td_get_wake_calls(), 1)
        self.assertEqual(lib.td_get_chime_calls(), 1)

    def test_above_threshold_fires_exactly_once(self):
        """A frame above the threshold fires exactly once: one chime and one
        wake, no more."""
        for score, threshold in [(0.90, 0.35), (0.8, 0.8)]:
            lib = self.library()
            det = self.detector(lib, [score], threshold=threshold)
            self.feed(lib, det)
            self.assertEqual(lib.td_get_wake_calls(), 1,
                             'score %.2f at threshold %.2f must fire' % (score, threshold))
            self.assertEqual(lib.td_get_chime_calls(), 1)

    def test_misbehaving_scorer_cannot_fire_the_device(self):
        """A scorer that returns a value outside [0, 1] (a NaN or a runaway
        reading) must not fire the device: only a sane reading that crosses
        the threshold is a detection."""
        lib = self.library()
        det = self.detector(lib, [float('nan'), -0.5, 1.5, 0.9], threshold=0.35)
        self.feed(lib, det)
        self.feed(lib, det)
        self.feed(lib, det)
        self.assertEqual(lib.td_get_wake_calls(), 0,
                         'NaN, negative and >1.0 scores must not fire')
        self.feed(lib, det)
        self.assertEqual(lib.td_get_wake_calls(), 1,
                         'a sane score after the bad ones must fire')

    # -- task 2: deaf guard ----------------------------------------------------
    def test_a_detection_is_single_shot_until_the_stream_ends(self):
        """Once the core fires it is deaf on its own (the microphone is now
        forwarded to the bridge): a HIGH score on the very next frame is not a
        second detection. After the stream ends the detector arms again."""
        lib = self.library()
        det = self.detector(lib, [0.9, 0.9, 0.9], threshold=0.35)
        self.feed(lib, det)                     # detection
        self.assertEqual(lib.td_get_wake_calls(), 1)
        self.feed(lib, det)                     # deaf: the answer is coming
        self.assertEqual(lib.td_get_wake_calls(), 1,
                         'a high score right after a fire must not re-fire')
        lib.wy_wake_stream_ended(det)           # the stream stopped
        self.feed(lib, det)                     # armed again (0 ms cooldown)
        self.assertEqual(lib.td_get_wake_calls(), 2,
                         'after the stream ends the detector must be armed again')

    def test_a_rejected_wake_keeps_the_core_armed(self):
        """If the wake seam REJECTS the fire (the session is paused or not
        armed, so wy_session_wake returns non-zero), the core must not go
        deaf: no stream was started, so the detector stays armed and the
        next crossing frame fires again. A rejected fire is a no-op for the
        device, not a reason to stop listening."""
        lib = self.library()
        det = self.detector(lib, [0.9, 0.9], threshold=0.35)
        lib.td_set_wake_result(-1)      # the session refuses the wake
        self.feed(lib, det)
        self.assertEqual(lib.td_get_wake_calls(), 1, 'the wake was still requested')
        self.assertFalse(lib.wy_wake_deaf(det),
                         'a rejected fire must not deafen the core')
        lib.td_set_wake_result(0)       # the session is ready now
        self.feed(lib, det)
        self.assertEqual(lib.td_get_wake_calls(), 2,
                         'the next crossing frame must fire again')
        self.assertTrue(lib.wy_wake_deaf(det), 'the accepted fire deafens')

    def test_a_null_wake_seam_degrades_to_staying_armed(self):
        """wy_wake.h:45 allows ANY seam to be NULL: "the core degrades -- no model, no chime,
        no wake -- rather than faulting." With the WAKE seam NULL there is no wake, so nothing
        opens a stream and wy_wake_stream_ended will never be called. The core must therefore
        stay ARMED and keep scoring -- NOT self-deafen over a stream that does not exist. The
        old code substituted 0 (the ACCEPTED value) for a missing wake seam and permanently
        deafened the core on the first crossing frame, wedging it silent forever with no stream
        to end. Degraded = the rejected-fire path: keep scoring."""
        lib = self.library()
        det = C.create_string_buffer(lib.wy_wake_size())
        self._keepalive.append(det)
        scores = (C.c_float * 2)(0.9, 0.9)
        self._keepalive.append(scores)
        lib.td_reset(scores, 2)
        # score + load + chime installed; WAKE seam is NULL (header-sanctioned).
        lib.wy_wake_set_seams(det, lib.td_addr_score(), lib.td_addr_load(),
                              lib.td_addr_chime(), None, None)
        self.assertEqual(lib.wy_wake_init(det, 0.35, 0, b'hey_hermes'), 0)
        self.assertTrue(lib.wy_wake_has_model(det), 'load seam gave the core a model')
        # First crossing frame: detects + chimes, but a NULL wake opens no stream, so the core
        # must NOT be deaf afterwards.
        self.assertEqual(self.feed(lib, det), 1)   # WY_WAKE_DETECTED
        self.assertEqual(lib.td_get_chime_calls(), 1)
        self.assertFalse(lib.wy_wake_deaf(det),
                         'a NULL wake seam opened no stream: the core must stay armed')
        # The core is still scoring: the very next crossing frame fires again.
        self.assertEqual(self.feed(lib, det), 1)   # WY_WAKE_DETECTED
        self.assertEqual(lib.td_get_score_calls(), 2,
                         'a degraded core keeps scoring; it must not wedge silent')
        self.assertEqual(lib.td_get_chime_calls(), 2)

    def test_deaf_while_streaming(self):
        """Explicit glue state: while the glue reports the stream running, a
        high score is consumed, and the core reports itself deaf."""
        lib = self.library()
        det = self.detector(lib, [0.9, 0.9], threshold=0.35)
        lib.wy_wake_set_streaming(det, 1)
        self.assertTrue(lib.wy_wake_deaf(det))
        self.feed(lib, det)
        self.assertEqual(lib.td_get_wake_calls(), 0,
                         'a high score while streaming must not fire')
        lib.wy_wake_set_streaming(det, 0)
        self.assertFalse(lib.wy_wake_deaf(det))
        self.feed(lib, det)
        self.assertEqual(lib.td_get_wake_calls(), 1)

    def test_deaf_during_playback_even_before_a_stream(self):
        """Playback of a locally rendered sound (the chime, an announcement)
        deems the core deaf even with no bridge stream running: a high score
        during playback is consumed, not a self-trigger."""
        lib = self.library()
        det = self.detector(lib, [0.9, 0.9], threshold=0.35)
        lib.wy_wake_set_playback_active(det, 1)
        self.assertTrue(lib.wy_wake_deaf(det))
        self.feed(lib, det)
        self.assertEqual(lib.td_get_wake_calls(), 0,
                         'a high score during local playback must not fire')
        lib.wy_wake_set_playback_active(det, 0)
        self.assertFalse(lib.wy_wake_deaf(det))
        self.feed(lib, det)
        self.assertEqual(lib.td_get_wake_calls(), 1,
                         'playback over: the same score now fires')

    def test_a_stalled_playback_ring_must_not_deafen_the_detector(self):
        """THE deadlock: the glue treats "the ring still holds audio" as "the
        speaker is sounding". A ring that is queued but never drained (the pump
        wedged, or the chime was queued and nothing played it) then keeps the
        detector deaf for ever -- and since nothing plays, the room hears nothing
        too. Both symptoms at once: silence and permanent deafness.

        Whether audio is still audible is a question about DRAINING, not about
        the ring being non-empty. A ring that has not drained for longer than the
        bound is a stalled queue, not playback."""
        lib = self.library()
        lib.wy_wake_playback_sounds.argtypes = [C.c_int, C.c_size_t, C.c_uint32]
        lib.wy_wake_playback_sounds.restype = C.c_int

        def sounds(active, queued, since_drained):
            # Call through the attribute every time: this harness caches attribute
            # access as separate _FuncPtr objects, so a captured alias can lose
            # the restype set above.
            return lib.wy_wake_playback_sounds(active, queued, since_drained)

        # The bridge's stream flag is authoritative while it is asserted.
        self.assertEqual(sounds(1, 0, 0), 1)
        # A ring that is draining is audible, whatever the level says.
        self.assertEqual(sounds(0, 4096, 0), 1)
        self.assertEqual(sounds(0, 4096, 1), 1)
        # Nothing queued and nothing active: quiet.
        self.assertEqual(sounds(0, 0, 0), 0)
        # THE REGRESSION: a non-empty ring that has not drained for a long time
        # is STALLED, not playing, and must not deafen anything.
        self.assertEqual(sounds(0, 4096, 100000), 0)
        self.assertEqual(sounds(1, 4096, 100000), 1,
                         'an asserted bridge stream stays authoritative')

    def test_the_chime_matches_the_reference_sound_set(self):
        """The device must reproduce the reference satellite's awake tone, not merely
        "a two-note chime". scripts/make_chimes.py is the source of truth: G4 392 Hz
        for 120 ms, an 18 ms gap, then C5 523 Hz for 170 ms. This reads the note
        frequencies back out of the rendered samples so a re-tuned constant cannot
        quietly drift away from the sound people already know."""
        lib = self.library()
        lib.wy_chime_render.argtypes = [C.POINTER(C.c_int16), C.c_size_t]
        lib.wy_chime_render.restype = C.c_size_t
        need = lib.wy_chime_render(None, 0)
        buf = (C.c_int16 * need)()
        got = lib.wy_chime_render(buf, need)
        self.assertEqual(got, need)

        rate = 16000.0

        def freq_of(start, length):
            # Zero crossings over a window: cheap and unambiguous for a pure tone.
            seg = list(buf[start:start + length])
            crossings = sum(1 for a, b in zip(seg, seg[1:]) if (a >= 0) != (b >= 0))
            return crossings * rate / (2.0 * length)

        # note 1: 120 ms at 16 kHz, sampled inside its ring (after the 15 ms attack)
        n1 = int(rate * 0.120)
        gap = int(rate * 0.018)
        n2 = int(rate * 0.170)
        self.assertEqual(n1 + gap + n2, need, 'the layout must be 120 ms + 18 ms + 170 ms')
        f1 = freq_of(int(rate * 0.030), int(rate * 0.070))
        f2 = freq_of(n1 + gap + int(rate * 0.030), int(rate * 0.070))
        self.assertAlmostEqual(f1, 392.0, delta=25.0,
                               msg='note 1 is G4 (392 Hz), not a re-tuned value')
        self.assertAlmostEqual(f2, 523.0, delta=25.0,
                               msg='note 2 is C5 (523 Hz), not a re-tuned value')

    def test_the_done_tone_is_the_same_pair_falling(self):
        """The done tone is the awake pair reversed: C5 then G4, so the two sound of a
        piece while still telling you which half of the exchange you are in."""
        lib = self.library()
        lib.wy_done_render.argtypes = [C.POINTER(C.c_int16), C.c_size_t]
        lib.wy_done_render.restype = C.c_size_t
        lib.wy_chime_render.argtypes = [C.POINTER(C.c_int16), C.c_size_t]
        lib.wy_chime_render.restype = C.c_size_t
        self.assertEqual(lib.wy_done_render(None, 0), lib.wy_chime_render(None, 0),
                         'the pair shares one envelope and length')

    def test_the_stall_bound_is_reachable_from_the_glue(self):
        """The bound has to be a real, named value the device glue can use to age
        its queue; a predicate with no bound is not a predicate."""
        lib = self.library()
        lib.wy_wake_playback_stall_frames.restype = C.c_uint32
        self.assertGreater(lib.wy_wake_playback_stall_frames(), 0)

    def test_cooldown_after_stream_end_eats_high_frames(self):
        """After stream_ended, the core stays deaf for the configured cooldown
        (milliseconds, counted in 10 ms feed frames: 160 samples = 10 ms).
        With a 20 ms cooldown the first two frames after the stream ends are
        consumed and the third scores again."""
        lib = self.library()
        det = self.detector(lib, [0.9, 0.9, 0.9, 0.9], threshold=0.35,
                            cooldown_ms=20)
        self.feed(lib, det)                     # the detection
        lib.wy_wake_stream_ended(det)           # the stream stopped
        self.assertTrue(lib.wy_wake_deaf(det),
                        'inside the post-stream cooldown the core is deaf')
        self.feed(lib, det)                     # 10 ms: still cooling down
        self.assertEqual(lib.td_get_wake_calls(), 1,
                         'a high frame inside the cooldown must not re-fire')
        self.feed(lib, det)                     # 20 ms: the last cooldown frame
        self.assertEqual(lib.td_get_wake_calls(), 1)
        self.feed(lib, det)                     # 30 ms: armed again
        self.assertEqual(lib.td_get_wake_calls(), 2,
                         'after the cooldown the detector is armed again')

    def test_zero_cooldown_rearms_immediately(self):
        """With a 0 ms cooldown, stream_ended returns the core to scoring on
        the very next frame (the default device setting)."""
        lib = self.library()
        det = self.detector(lib, [0.9, 0.9], threshold=0.35, cooldown_ms=0)
        self.feed(lib, det)
        lib.wy_wake_stream_ended(det)
        self.assertFalse(lib.wy_wake_deaf(det),
                         '0 ms cooldown: straight back to scoring')
        self.feed(lib, det)
        self.assertEqual(lib.td_get_wake_calls(), 2)

    def test_deaf_is_reported_for_the_stream_and_cooldown_states(self):
        """The deaf accessor is the core's own answer to 'am I scoring?': true
        while the stream is running, true while a local sound plays, true
        during the post-stream cooldown, false when idle. The glue uses it to
        keep a new chime from clashing with a stream and to report the state
        on the console."""
        lib = self.library()
        det = self.detector(lib, [0.5], threshold=0.35, cooldown_ms=20)
        self.assertFalse(lib.wy_wake_deaf(det))
        lib.wy_wake_set_streaming(det, 1)
        self.assertTrue(lib.wy_wake_deaf(det))
        lib.wy_wake_set_streaming(det, 0)
        self.assertFalse(lib.wy_wake_deaf(det))
        lib.wy_wake_set_playback_active(det, 1)
        self.assertTrue(lib.wy_wake_deaf(det))
        lib.wy_wake_set_playback_active(det, 0)
        self.assertFalse(lib.wy_wake_deaf(det))
        lib.wy_wake_stream_ended(det)
        self.assertTrue(lib.wy_wake_deaf(det), 'cooldown starts deaf')
        self.feed(lib, det)
        self.feed(lib, det)
        self.assertFalse(lib.wy_wake_deaf(det), 'cooldown over')

    # -- task 3: chime-then-wake ordering --------------------------------------
    def test_chime_is_requested_before_wake(self):
        """On a detection the chime seam is invoked BEFORE the wake seam, and
        each exactly once: the 'awake' tone is what tells the person the device
        heard them, so it must sound before the run-satellite sequence
        starts."""
        lib = self.library()
        det = self.detector(lib, [0.9], threshold=0.35)
        self.feed(lib, det)
        self.assertEqual(lib.td_get_chime_calls(), 1)
        self.assertEqual(lib.td_get_wake_calls(), 1)
        self.assertEqual(lib.td_chime_then_wake(), 1,
                         'chime must come first, both exactly once')

    def test_a_non_firing_feed_invokes_neither(self):
        """No detection, no chime, no wake: a sub-threshold frame consumes
        silence."""
        lib = self.library()
        det = self.detector(lib, [0.10], threshold=0.35)
        self.feed(lib, det)
        self.assertEqual(lib.td_get_chime_calls(), 0)
        self.assertEqual(lib.td_get_wake_calls(), 0)
        self.assertEqual(lib.td_chime_then_wake(), 0)

    # -- task 4: threshold + model from config ---------------------------------
    def test_load_seam_called_once_with_the_config_model_name(self):
        """Init asks the load seam for the model named by the config wake_word
        (the model SELECTOR in storage -- distinct from the detection name the
        session reports), exactly once, at init."""
        lib = self.library()
        self.detector(lib, [], model_name=b'hey_hermes')
        self.assertEqual(lib.td_get_load_calls(), 1)
        self.assertEqual(lib.td_loaded_name_str(), b'hey_hermes')

    def test_has_model_true_on_a_successful_load(self):
        lib = self.library()
        det = self.detector(lib, [])
        self.assertTrue(lib.wy_wake_has_model(det))

    def test_load_failure_comes_up_model_less_and_never_fires(self):
        """A failed model load is not an init failure: the core comes up armed
        but mute -- it consumes frames, never calls the scorer, and never
        fires. This is the corrupt-model fallback (and the pre-#110 state)."""
        lib = self.library()
        det = self.detector(lib, [0.9, 0.9], threshold=0.35)
        lib.td_set_load_fail(1)
        self.assertEqual(lib.wy_wake_init(det, 0.35, 0, b'hey_hermes'), 0)
        self.assertFalse(lib.wy_wake_has_model(det))
        self.assertFalse(lib.wy_wake_deaf(det), 'model-less is not deaf: armed, mute')
        self.feed(lib, det)
        self.feed(lib, det)
        self.assertEqual(lib.td_get_wake_calls(), 0)
        self.assertEqual(lib.td_get_score_calls(), 0,
                         'a model-less core must not even call the scorer')

    def test_no_load_seam_comes_up_model_less(self):
        """No load seam at all (the glue has no model partition to read): same
        contract -- init succeeds, the core never fires."""
        lib = self.library()
        det = self.detector(lib, [0.9], with_load=False)
        self.assertFalse(lib.wy_wake_has_model(det))
        self.feed(lib, det)
        self.assertEqual(lib.td_get_wake_calls(), 0)
        self.assertEqual(lib.td_get_score_calls(), 0)

    def test_init_rejects_out_of_range_thresholds(self):
        """The core accepts any threshold in [0, 1] (the config plane bounds it
        tighter, to [0.05, 0.99]) and rejects everything else, including
        NaN: a bad value is a bad argument, not a silent comparator."""
        lib = self.library()
        for bad in (1.5, -0.1, float('nan')):
            buf = C.create_string_buffer(lib.wy_wake_size())
            self._keepalive.append(buf)
            lib.td_reset(None, 0)
            lib.wy_wake_set_seams(buf, lib.td_addr_score(), lib.td_addr_load(),
                                  lib.td_addr_chime(), lib.td_addr_wake(), None)
            self.assertEqual(lib.wy_wake_init(buf, bad, 0, b'hey_hermes'), -1)
        # The inclusive bounds are accepted.
        self.detector(lib, [], threshold=0.0)
        self.detector(lib, [], threshold=1.0)

    def test_init_rejects_a_null_model_name(self):
        lib = self.library()
        buf = C.create_string_buffer(lib.wy_wake_size())
        self._keepalive.append(buf)
        lib.td_reset(None, 0)
        lib.wy_wake_set_seams(buf, lib.td_addr_score(), lib.td_addr_load(),
                              lib.td_addr_chime(), lib.td_addr_wake(), None)
        self.assertEqual(lib.wy_wake_init(buf, 0.35, 0, None), -1)


class ChimeRenderTests(unittest.TestCase):
    """D4 (increment 4): the "awake" chime as a rendered sample array. Pure
    math, no file dependency, so the device never needs a tone file and the
    render is host-testable. The device glue streams the samples to the I2S TX."""

    def library(self):
        source = COMP / 'wy_wake/wy_wake.c'
        self.assertTrue(source.exists(), 'wake core missing: %s' % source)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'chime.so'
        subprocess.run(
            ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
             '-I' + str(COMP / 'wy_wake'),
             str(source), '-lm', '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.wy_chime_render.argtypes = [C.c_void_p, C.c_size_t]
        lib.wy_chime_render.restype = C.c_size_t
        return lib

    def render(self, lib, cap):
        """Render into a cap-sample buffer, returning (written, samples)."""
        buf = C.create_string_buffer(cap * 2) if cap else None
        written = lib.wy_chime_render(buf, cap)
        samples = None
        if buf is not None and written:
            samples = struct.unpack('<%dh' % written, buf.raw[:written * 2])
        return written, samples

    def test_query_reports_a_positive_fixed_length(self):
        """out == NULL is a length query: it reports how many samples the chime
        needs without writing anything, and the answer is a fixed, positive,
        16-bit-mono length (a short tone, not a whole second)."""
        lib = self.library()
        needed = lib.wy_chime_render(None, 0)
        self.assertGreater(needed, 0)
        # 4928 samples at 16 kHz = 308 ms, matching the reference sound set exactly
        # (scripts/make_chimes.py: G4 120 ms + 18 ms gap + C5 170 ms).
        self.assertEqual(needed, 4928,
                         'the awake chime must be the reference 308 ms design')
        # The query is repeatable and never writes.
        self.assertEqual(lib.wy_chime_render(None, 0), needed)

    def test_render_fills_min_cap_needed(self):
        """With a buffer, the render writes min(cap, needed) samples and returns
        that count: a small cap is honored (no overflow), a big cap fills
        exactly the chime and no more."""
        lib = self.library()
        needed = lib.wy_chime_render(None, 0)
        small, _ = self.render(lib, needed // 2)
        self.assertEqual(small, needed // 2, 'a small cap must be honored, not overflowed')
        full, _ = self.render(lib, needed + 1000)
        self.assertEqual(full, needed, 'a big cap renders exactly the chime')

    def test_render_is_deterministic(self):
        """Same render, same bytes: the tone is a fixed function of its
        parameters, so the device and the host always produce the identical
        sound (and a corrupt or drifting render is a visible diff)."""
        lib = self.library()
        needed = lib.wy_chime_render(None, 0)
        _, a = self.render(lib, needed)
        _, b = self.render(lib, needed)
        self.assertTrue(a is not None and b is not None)
        # a/b are non-None (asserted above); the list comprehensions are the comparison.
        self.assertEqual([v for v in a], [v for v in b])  # type: ignore[union-attr]

    def test_render_has_real_content_and_fades(self):
        """The rendered tone is not all-silence and not clipping: it has
        non-zero samples, stays within the int16 range, and (having an attack
        and a decay) is quieter at the very end than at its loudest point."""
        lib = self.library()
        needed = lib.wy_chime_render(None, 0)
        _, s = self.render(lib, needed)
        self.assertTrue(s is not None and needed > 0)
        vals = [v for v in s]
        self.assertTrue(any(v != 0 for v in vals), 'the chime must not be silence')
        self.assertTrue(all(-32768 <= v <= 32767 for v in vals), 'no int16 overflow')
        peak = max(abs(v) for v in vals)
        self.assertGreater(peak, 0)
        tail = max(abs(v) for v in vals[-needed // 8:])
        self.assertLess(tail, peak, 'the tone must decay, not end at peak')


if __name__ == '__main__':
    unittest.main()