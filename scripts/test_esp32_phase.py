#!/usr/bin/env python3
"""The turn must announce its phases, or the lights have nothing to show.

The Wyoming session core does not know what a light is. It announces WHICH PHASE a turn is in
through an injected callback, and the device glue decides what that looks like. This tests the
announcement half: drive a whole turn and assert the exact sequence handed to the presentation.

The sequence matters as much as the events. A thinking glow that appears after the answer starts
is worse than no glow at all, because it claims the device is thinking while it is talking.
"""
import ctypes as C
import importlib.util
import io
from pathlib import Path
import unittest
from wyoming.event import Event, write_event

ROOT = Path(__file__).resolve().parents[1]

LISTENING, TRANSCRIPT, SPEAKING, IDLE = 1, 2, 3, 4

PHASES = C.CFUNCTYPE(None, C.c_void_p, C.c_int)


class PhaseSequenceTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'session_harness', ROOT / 'scripts/test_esp32_session.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.harness = module.SessionTests()
        self.addCleanup(self.harness.doCleanups)
        self.lib = self.harness.library()
        self.IO = module.IO
        # ctypes must know this argument is a function pointer, or the callback is not delivered
        # at all -- it passes an integer and the core stores a pointer it can never call.
        self.lib.wy_session_set_phase_notify.argtypes = [C.c_void_p, PHASES, C.c_void_p]
        self.lib.wy_session_set_phase_notify.restype = None
        self.seen = []

    def _phase_cb(self):
        @PHASES
        def cb(_user, phase):
            self.seen.append(phase)
        self._cb = cb                       # keep the trampoline alive
        return cb

    def _session(self, events):
        lib = self.lib
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'phase-fixture', b'boot'), 0)
        lib.wy_session_set_phase_notify(session, self._phase_cb(), None)

        source = io.BytesIO()
        for event in events:
            write_event(event, source)
        source.seek(0)

        @self.IO
        def read(_ctx, ptr, size):
            raw = source.read(size)
            C.memmove(ptr, raw, len(raw))
            return len(raw)

        @self.IO
        def write(_ctx, ptr, size):
            return size

        for _ in range(len(events)):
            lib.wy_session_step(session, read, write, None)
        self.sess = session
        return session

    def _armed(self):
        return self._session([
            Event(type='describe'),
            Event(type='hermes.register.ok',
                  data={'device_id': 'phase-fixture', 'protocol_version': 1}),
            Event(type='run-satellite'),
        ])

    def test_a_turn_announces_thinking_then_speaking_then_idle(self):
        session = self._armed()
        self.lib.wy_session_step(session, self._source_for([
            Event(type='transcript', data={'text': 'what time is it'}),
        ]), self._writer(), None)
        self.lib.wy_session_step(session, self._source_for([
            Event(type='audio-start',
                  data={'rate': 22050, 'width': 2, 'channels': 1}),
        ]), self._writer(), None)
        self.lib.wy_session_step(session, self._source_for([
            Event(type='audio-stop'),
        ]), self._writer(), None)

        self.assertEqual(self.seen, [TRANSCRIPT, SPEAKING, IDLE],
                         'a turn must announce its phases in order')

    def test_thinking_is_announced_before_the_answer_starts(self):
        """The glow has to land in the gap between the person stopping and the first word coming
        back. Announcing it after audio-start would claim the device is thinking while it talks."""
        self._armed()
        self.lib.wy_session_step(self.sess, self._source_for([
            Event(type='transcript', data={'text': 'hello'}),
        ]), self._writer(), None)
        self.assertEqual(self.seen, [TRANSCRIPT], 'thinking starts the moment words arrive')
        self.lib.wy_session_step(self.sess, self._source_for([
            Event(type='audio-start', data={'rate': 22050, 'width': 2, 'channels': 1}),
        ]), self._writer(), None)
        self.assertEqual(self.seen[-1], SPEAKING)

    def test_an_unplayable_audio_start_does_not_announce_speaking(self):
        """A stream in the wrong format is drained, not armed. Claiming the device is speaking
        when it is about to throw the audio away would be a lie the ring tells every time."""
        self._armed()
        for ev in (Event(type='transcript', data={'text': 'hi'}),
                   Event(type='audio-start', data={'rate': 16000, 'width': 2, 'channels': 1})):
            self.lib.wy_session_step(self.sess, self._source_for([ev]), self._writer(), None)
        self.assertNotIn(SPEAKING, self.seen, 'an unarmed stream is not speech')

    def test_a_stop_with_no_armed_stream_announces_nothing(self):
        self._armed()
        self.lib.wy_session_step(self.sess, self._source_for([
            Event(type='audio-stop'),
        ]), self._writer(), None)
        self.assertEqual(self.seen, [], 'an unearned idle would blank a light mid-turn')

    def test_no_callback_is_not_an_error(self):
        """The core must not require a presentation: a device with no lights still has turns."""
        lib = self.lib
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'quiet', b'boot'), 0)
        self.sess = session
        self._step([Event(type='describe')])
        self.assertNotEqual(lib.wy_session_state(session), 0, 'the session still works unlit')

    def test_clearing_the_callback_stops_announcing(self):
        session = self._armed()
        self.lib.wy_session_set_phase_notify(session, PHASES(0), None)
        self.lib.wy_session_step(session, self._source_for([
            Event(type='transcript', data={'text': 'hi'}),
        ]), self._writer(), None)
        self.assertEqual(self.seen, [], 'a cleared callback announces nothing')

    # --- helpers ---

    def _source_for(self, events):
        source = io.BytesIO()
        for event in events:
            write_event(event, source)
        source.seek(0)

        @self.IO
        def read(_ctx, ptr, size):
            raw = source.read(size)
            C.memmove(ptr, raw, len(raw))
            return len(raw)
        self._reads = read
        return read

    def _writer(self):
        @self.IO
        def write(_ctx, ptr, size):
            return size
        self._writes = write
        return write

    def _step(self, events):
        self.lib.wy_session_step(self.sess, self._source_for(events), self._writer(), None)


if __name__ == '__main__':
    unittest.main()
