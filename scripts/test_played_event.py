#!/usr/bin/env python3
"""The device must report `played`, or the Gateway waits for an answer that never comes.

After every reply the Gateway waits for the device's `played` event. Firmware that
does not emit it parks the whole conversation in that wait -- measured at 20 s of
dead air after every single answer, and at a "works once then nothing" failure
while every wake inside the wait was dismissed as an echo.

This drives the REAL production session core and the REAL Wyoming writer, so it is
an interop check rather than a self-round-trip.
"""
import asyncio
import ctypes as C
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from wyoming.event import read_event

ROOT = Path(__file__).resolve().parents[1]


class PlayedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'session_harness', ROOT / 'scripts/test_esp32_session.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.harness = module.SessionTests()
        self.addCleanup(self.harness.doCleanups)
        self.lib = self.harness.library()
        self.IO = module.IO
        self.lib.wy_session_send_played.argtypes = [C.c_void_p, self.IO, C.c_void_p]
        self.lib.wy_session_send_played.restype = C.c_int

    async def test_played_is_sent_as_one_control_frame(self):
        lib = self.lib
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'played-fixture', b'boot'), 0)
        out = io.BytesIO()

        @self.IO
        def write(_ctx, ptr, size):
            out.write(C.string_at(ptr, size))
            return size

        self.assertEqual(lib.wy_session_send_played(session, write, None), 0)
        out.seek(0)
        event = read_event(out)
        self.assertIsNotNone(event)
        self.assertEqual(event.type, 'played')
        self.assertIsNone(read_event(out), 'played is exactly one frame')

    async def test_a_missing_link_is_refused_not_sent(self):
        lib = self.lib
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'played-fixture', b'boot'), 0)
        no_link = C.cast(None, self.IO)   # a real NULL function pointer
        self.assertNotEqual(lib.wy_session_send_played(session, no_link, None), 0)


if __name__ == '__main__':
    unittest.main()
