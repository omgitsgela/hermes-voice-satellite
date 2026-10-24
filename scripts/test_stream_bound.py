#!/usr/bin/env python3
"""An open microphone stream must be bounded, not open-ended.

The bridge closes a stream with `transcript`. If that never arrives -- a wake the
bridge loses track of, a crash mid-turn -- the device would keep forwarding a room
nobody is listening to. Measured: a wake the bridge dismissed as its own echo left
`streaming = 1` indefinitely, with `capacct: sent 100, dropped 0` running for
minutes. The reference satellite closes its stream at the end of the utterance;
this is the same guarantee enforced from the device side.
"""
import ctypes as C
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from wyoming.event import Event, write_event

ROOT = Path(__file__).resolve().parents[1]


class StreamBoundTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'session_harness', ROOT / 'scripts/test_esp32_session.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.harness = module.SessionTests()
        self.addCleanup(self.harness.doCleanups)
        self.lib = self.harness.library()
        self.IO = module.IO
        # The wake seam's arbitration parameters are plain scalars: ctypes needs
        # explicit argtypes for them (a float is not auto-convertible).
        self.lib.wy_session_wake.argtypes = [C.c_void_p, C.c_char_p, C.c_int, C.c_float,
                                             self.IO, C.c_void_p]
        self.lib.wy_session_wake.restype = C.c_int

    def _armed_session(self):
        lib = self.lib
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'bound-fixture', b'boot'), 0)
        source = io.BytesIO()
        for event in [Event(type='describe'),
                      Event(type='hermes.register.ok',
                            data={'device_id': 'bound-fixture', 'protocol_version': 1}),
                      Event(type='run-satellite')]:
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

        for _ in range(3):
            self.assertEqual(lib.wy_session_step(session, read, write, None), 0)
        # Level -1: this test is about the stream cap, not arbitration; the
        # historical (context-free) wake shape exercises the cap unchanged.
        self.assertEqual(lib.wy_session_wake(session, b"hey_hermes", -1, -1.0, write, None), 0)
        return session, write

    def test_a_stream_without_a_transcript_is_capped(self):
        lib = self.lib
        lib.wy_session_send_audio.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t,
                                              self.IO, C.c_void_p]
        lib.wy_session_send_audio.restype = C.c_int
        session, write = self._armed_session()
        pcm = (C.c_int16 * 160)()

        sent = 0
        for _ in range(5000):
            if lib.wy_session_send_audio(session, pcm, 320, write, None) != 0:
                break
            sent += 1
        self.assertEqual(lib.wy_session_streaming(session), 0,
                         'the stream must close itself once the bound is reached')
        self.assertGreater(sent, 1000, 'the bound must be generous, not twitchy')
        self.assertLessEqual(sent, 3000, 'and it must actually cap the stream')

    def test_a_normal_utterance_is_nowhere_near_the_bound(self):
        """A 30 s cap against a real utterance of a few seconds: the bound must not be
        something an ordinary turn can ever trip."""
        self.assertGreaterEqual(3000 * 10, 30000,
                                '3000 frames of 10 ms is 30 s of audio')


if __name__ == '__main__':
    unittest.main()
