#!/usr/bin/env python3
"""Increment 4, task 5: the wake core's fire drives the REAL session core.

Links the production wake core (wy_wake) with the production session core
(wyoming_session + its json/wire/audio deps) plus the host glue
(test_esp32_wake_glue.c), whose wake seam calls wy_session_wake -- exactly
the device glue's wiring. It proves the integration contract the plan states:

  1. A sub-threshold frame produces nothing on the wire.
  2. A crossing frame fires: the chime seam is invoked, then the wake seam
     reaches the session core, which emits detection -> run-pipeline ->
     streaming-started IN ORDER and opens the stream (streaming() == 1) while
     the device stays armed (state 3).
  3. A second detection while the stream is running is a NO-OP on the wire:
     the core is self-deaf after a fire, and the session core independently
     drops a wake that lands while streaming -- the device cannot re-wake on
     its own answer.
  4. The Gateway's transcript stops the stream (streaming-stopped, still armed);
     after wy_wake_stream_ended (0 ms cooldown) the detector re-arms and a
     crossing frame starts a FRESH stream: a second full three-event sequence.

Requires the repo .venv (the wyoming package, as in test_esp32_session.py):
    ./.venv/bin/python scripts/test_esp32_wake_integration.py
"""
import ctypes as C
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from wyoming.event import Event, read_event, write_event

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / 'firmware/esp32/components'
DRIVER = ROOT / 'scripts/test_esp32_wake_driver.c'
GLUE = ROOT / 'scripts/test_esp32_wake_glue.c'

# The session's IO callback: int (void *ctx, void *target, size_t length).
IO = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.c_size_t)


class WakeSessionIntegrationTests(unittest.TestCase):
    def library(self):
        """Compile wake core + session core (+ json/wire/audio deps) + driver +
        glue into one host .so."""
        parser = COMP / 'wyoming_json'
        includes = [COMP / 'wyoming_wire', parser, parser / 'vendor/cjson',
                    COMP / 'wyoming_session', COMP / 'wyoming_audio', COMP / 'wy_wake']
        sources = [COMP / 'wy_wake/wy_wake.c', COMP / 'wyoming_wire/wyoming_wire.c',
                   parser / 'wyoming_json.c', parser / 'vendor/cjson/cJSON.c',
                   COMP / 'wyoming_session/wyoming_session.c',
                   COMP / 'wyoming_audio/wyoming_audio.c', str(DRIVER), str(GLUE)]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'wake_session.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        '-DCJSON_NESTING_LIMIT=16', *['-I' + str(p) for p in includes],
                        *sources, '-lm', '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        # Wake core API.
        lib.wy_wake_size.restype = C.c_size_t
        lib.wy_wake_set_seams.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p,
                                          C.c_void_p, C.c_void_p, C.c_void_p]
        lib.wy_wake_set_seams.restype = None
        lib.wy_wake_init.argtypes = [C.c_void_p, C.c_double, C.c_uint, C.c_char_p]
        lib.wy_wake_init.restype = C.c_int
        lib.wy_wake_feed.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t]
        lib.wy_wake_feed.restype = C.c_int
        lib.wy_wake_stream_ended.argtypes = [C.c_void_p]
        lib.wy_wake_stream_ended.restype = None
        lib.wy_wake_deaf.argtypes = [C.c_void_p]
        lib.wy_wake_deaf.restype = C.c_bool
        # Session core API.
        lib.wy_session_size.restype = C.c_size_t
        lib.wy_session_init.argtypes = [C.c_void_p, C.c_char_p, C.c_char_p]
        lib.wy_session_init.restype = C.c_int
        lib.wy_session_state.argtypes = [C.c_void_p]
        lib.wy_session_state.restype = C.c_int
        lib.wy_session_step.argtypes = [C.c_void_p, IO, IO, C.c_void_p]
        lib.wy_session_step.restype = C.c_int
        lib.wy_session_streaming.argtypes = [C.c_void_p]
        lib.wy_session_streaming.restype = C.c_int
        lib.wy_session_wake.argtypes = [C.c_void_p, C.c_char_p, C.c_int, C.c_float, IO, C.c_void_p]
        lib.wy_session_wake.restype = C.c_int
        # Driver + glue accessors.
        lib.td_reset.argtypes = [C.POINTER(C.c_float), C.c_int]
        lib.td_reset.restype = None
        for name in ('td_get_score_calls', 'td_get_chime_calls', 'td_get_wake_calls',
                     'td_get_load_calls'):
            getattr(lib, name).restype = C.c_int
        for name in ('td_addr_score', 'td_addr_load', 'td_addr_chime'):
            getattr(lib, name).restype = C.c_void_p
        lib.iw_addr_wake_seam.restype = C.c_void_p
        return lib

    def armed_session(self, lib):
        """A session driven through the handshake to the armed state (3):
        describe, register.ok, run-satellite. Returns (session, output)."""
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        source = io.BytesIO()
        for event in [Event(type='describe'),
                      Event(type='hermes.register.ok',
                            data={'device_id': 'control-fixture', 'protocol_version': 1}),
                      Event(type='run-satellite')]:
            write_event(event, source)
        source.seek(0)
        output = io.BytesIO()

        @IO
        def reader(ctx, target, length):
            chunk = source.read(min(5, length))
            C.memmove(target, chunk, len(chunk))
            return len(chunk)

        @IO
        def writer(ctx, target, length):
            output.write(C.string_at(target, length))
            return length

        for expected in (1, 2, 3):
            self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
            self.assertEqual(lib.wy_session_state(session), expected)
        # The writer appended at the buffer's current position, so rewind before
        # consuming the handshake's info + hermes.register; the position then lands
        # exactly where the wake sequence will start.
        output.seek(0)
        self.assertIsNotNone(read_event(output))
        self.assertIsNotNone(read_event(output))
        self.assertIsNone(read_event(output))
        return session, output, reader, writer

    def armed_detector(self, lib, session, writer, scores, threshold=0.35,
                       cooldown_ms=0):
        """A fully-wired detector whose wake seam is the REAL session wake:
        the core's user pointer is the glue's iw_wake_ctx (session, detection
        name, writer, writer ctx)."""

        class IwCtx(C.Structure):
            _fields_ = [('session', C.c_void_p), ('name', C.c_char_p),
                        ('write', IO), ('ctx', C.c_void_p), ('core', C.c_void_p)]

        det = C.create_string_buffer(lib.wy_wake_size())
        self._det_keepalive = det
        ctx = IwCtx(C.cast(session, C.c_void_p), b'hey-hermes', writer, None,
                    C.cast(det, C.c_void_p))
        self._ctx_keepalive = ctx
        self._writer_keepalive = writer
        lib.td_reset((C.c_float * len(scores))(*scores) if scores else None,
                     len(scores) if scores else 0)
        lib.wy_wake_set_seams(det, lib.td_addr_score(), lib.td_addr_load(),
                              lib.td_addr_chime(), lib.iw_addr_wake_seam(), C.byref(ctx))
        self.assertEqual(lib.wy_wake_init(det, threshold, cooldown_ms, b'hey_hermes'), 0)
        return det

    def feed(self, lib, det, frames=160, amplitude=0):
        """One 10 ms chunk through the core. `amplitude` fills the buffer with a
        sine-like alternating pattern at that sample amplitude (0 = silence),
        so a test can give the core a room loudness to measure."""
        pcm = C.create_string_buffer(frames * 2)
        if amplitude:
            samples = (C.c_int16 * frames)(
                *[amplitude if i % 2 == 0 else -amplitude for i in range(frames)])
            C.memmove(pcm, samples, frames * 2)
        return lib.wy_wake_feed(det, pcm, frames)

    def read_wire(self, output, count):
        """Read up to `count` events from the CURRENT output position (the test
        has already positioned the cursor where the sequence starts), returning
        the events or None when the link is quiet (no event there)."""
        events = []
        for _ in range(count):
            event = read_event(output)
            if event is None:
                break
            events.append(event)
        return events or None

    def test_detector_fire_opens_the_stream_via_the_real_session(self):
        """The core's crossing frame reaches the session core through the wake
        seam: the chime is requested, then the session emits detection ->
        run-pipeline -> streaming-started IN ORDER, the stream is open, and the
        device stays armed. A sub-threshold frame before it changes nothing."""
        lib = self.library()
        session, output, reader, writer = self.armed_session(lib)
        det = self.armed_detector(lib, session, writer, [0.10, 0.90])
        self.assertEqual(self.feed(lib, det), 0, 'sub-threshold: consumed, quiet')
        self.assertIsNone(self.read_wire(output, 1), 'a sub-threshold frame is silent')
        self.assertEqual(lib.td_get_chime_calls(), 0)

        start = output.tell()
        self.assertEqual(self.feed(lib, det), 1, 'crossing frame: a detection fired')
        self.assertEqual(lib.td_get_chime_calls(), 1, 'the chime was requested')
        output.seek(start)
        events = self.read_wire(output, 3)
        self.assertIsNotNone(events, 'the fire must reach the wire')
        self.assertEqual([e.type for e in events],
                         ['detection', 'run-pipeline', 'streaming-started'])
        self.assertEqual(events[0].data['name'], 'hey-hermes')
        self.assertEqual(events[1].data, {'start_stage': 'asr', 'end_stage': 'tts',
                                          'restart_on_end': False})
        self.assertEqual(lib.wy_session_streaming(session), 1, 'the stream is open')
        self.assertEqual(lib.wy_session_state(session), 3, 'the device stays armed')
        self.assertTrue(lib.wy_wake_deaf(det), 'fired: the core is self-deaf')

    def test_second_detection_while_streaming_is_dropped_on_the_wire(self):
        """After a fire, another crossing frame must produce NOTHING new: the
        core is self-deaf (the microphone is now forwarded to the gateway) and
        the session core independently drops a wake that lands while streaming
        -- the device cannot re-wake on its own answer."""
        lib = self.library()
        session, output, reader, writer = self.armed_session(lib)
        det = self.armed_detector(lib, session, writer, [0.90, 0.90, 0.90])
        start = output.tell()
        self.feed(lib, det)
        output.seek(start)
        events = self.read_wire(output, 3)
        self.assertEqual([e.type for e in events],
                         ['detection', 'run-pipeline', 'streaming-started'])
        quiet_start = output.tell()
        self.feed(lib, det)                     # the device's own answer is playing
        self.feed(lib, det)
        new = self.read_wire(output, 1)
        self.assertIsNone(new, 'a high frame while streaming must not re-fire')
        output.seek(quiet_start)
        self.assertEqual(output.read(1), b'', 'no new bytes on the link at all')
        self.assertEqual(lib.td_get_chime_calls(), 1, 'still exactly one chime')
        self.assertEqual(lib.wy_session_streaming(session), 1)

    def test_detector_fire_carries_the_measured_level_in_the_detection_context(self):
        """Issue #32 end to end on the wire: a loud frame that fires produces the same
        detection -> run-pipeline -> streaming-started sequence, and the detection event
        carries context.wake_level -- the number the core froze from the frame's amplitude.
        A fire with nothing measured (no armed scored frame yet, e.g. amplitude 0 through a
        core whose measure was never seeded because... it WAS seeded by the silence frame:
        silence is a measurement) is asserted separately for the no-context shape below."""
        import math
        lib = self.library()
        lib.wy_wake_last_level.argtypes = [C.c_void_p]
        lib.wy_wake_last_level.restype = C.c_int
        session, output, reader, writer = self.armed_session(lib)
        det = self.armed_detector(lib, session, writer, [0.90])
        start = output.tell()
        self.assertEqual(self.feed(lib, det, amplitude=12000), 1)
        output.seek(start)
        events = self.read_wire(output, 3)
        self.assertEqual([e.type for e in events],
                         ['detection', 'run-pipeline', 'streaming-started'])
        data = events[0].data
        self.assertEqual(data['name'], 'hey-hermes')
        context = data.get('context')
        self.assertIsNotNone(context, 'a measured fire must attach the arbitration context')
        level = context.get('wake_level')
        self.assertIsInstance(level, int)
        db = 20.0 * math.log10((12000 / 32768.0))
        expected = round((db + 60.0) / 60.0 * 100.0)
        self.assertIn(level, (expected, expected + 1),
                      "the wire level must match the core's frozen mapping (got %r want ~%d)"
                      % (level, expected))
        self.assertEqual(level, lib.wy_wake_last_level(det),
                         "the wire value and the core's report are the same frozen number")
        score = context.get('wake_score')
        self.assertIsNotNone(score, "the crossing score rides as the arbitration tiebreaker")
        self.assertAlmostEqual(score, 0.90, places=3,
                               msg="the score on the wire is the score the core fired on")

    def test_unmeasured_fire_keeps_the_historical_context_free_shape(self):
        """A forced fire with nothing measured sends the OLD detection frame exactly
        ({'name': ...} and no context at all): old Gateway builds and the Gateway's
        level-less fallback both keep working unchanged."""
        lib = self.library()
        session, output, reader, writer = self.armed_session(lib)
        det = self.armed_detector(lib, session, writer, [0.90])
        # Force the fire through the driver's forced-fire hook is not available here;
        # instead reach the fire WITHOUT any scored frame: streaming off, but the core's
        # measure only seeds on an armed SCORED frame -- a frame below threshold still
        # scores (the seam runs), so use the no-model route instead: with_load happens in
        # the unit test. On the integration side, the historical shape is proved by the
        # session core itself: wy_session_wake with level -1 (the wake-now path).
        start = output.tell()
        self.assertEqual(lib.wy_session_wake(session, b'hey-hermes', -1, -1.0, writer, None), 0)
        output.seek(start)
        events = self.read_wire(output, 3)
        self.assertEqual([e.type for e in events],
                         ['detection', 'run-pipeline', 'streaming-started'])
        self.assertEqual(events[0].data, {'name': 'hey-hermes'},
                         'a level-less detection must be byte-identical to the historical shape')

    def test_transcript_stops_and_the_detector_rearms(self):
        """The Gateway's transcript stops the stream (streaming-stopped, device
        still armed); after wy_wake_stream_ended with a 0 ms cooldown the core
        re-arms, and a crossing frame starts a FRESH stream -- a second full
        three-event sequence."""
        lib = self.library()
        session, output, reader, writer = self.armed_session(lib)
        det = self.armed_detector(lib, session, writer, [0.90, 0.90])
        start = output.tell()
        self.feed(lib, det)
        output.seek(start)
        self.assertEqual([e.type for e in self.read_wire(output, 3)],
                         ['detection', 'run-pipeline', 'streaming-started'])
        # The transcript: the utterance is over.
        source = io.BytesIO()
        write_event(Event(type='transcript', data={'text': 'hello'}), source)
        source.seek(0)

        @IO
        def reader2(ctx, target, length):
            chunk = source.read(min(5, length))
            C.memmove(target, chunk, len(chunk))
            return len(chunk)

        stop_start = output.tell()
        self.assertEqual(lib.wy_session_step(session, reader2, writer, None), 0)
        self.assertEqual(lib.wy_session_state(session), 3, 'still armed')
        self.assertEqual(lib.wy_session_streaming(session), 0, 'the stream is closed')
        output.seek(stop_start)
        stopped = self.read_wire(output, 1)
        self.assertEqual([e.type for e in stopped], ['streaming-stopped'])

        lib.wy_wake_stream_ended(det)
        self.assertFalse(lib.wy_wake_deaf(det), '0 ms cooldown: re-armed at once')
        start = output.tell()
        self.feed(lib, det)
        output.seek(start)
        events = self.read_wire(output, 3)
        self.assertEqual([e.type for e in events],
                         ['detection', 'run-pipeline', 'streaming-started'],
                         'after the stream ends a crossing frame starts a fresh stream')
        self.assertEqual(lib.td_get_chime_calls(), 2)
        self.assertEqual(lib.wy_session_streaming(session), 1)


if __name__ == '__main__':
    unittest.main()
