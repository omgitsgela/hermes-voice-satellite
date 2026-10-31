#!/usr/bin/env python3
"""Offline bridge-to-production-firmware framing checks. No audio devices or synthesis."""
import asyncio
import ctypes as C
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from satellite_gateway import tts
from satellite_gateway.config import SND_FORMAT
from satellite_gateway.satellite import SatelliteClient
from wyoming.audio import AudioChunk
from wyoming.event import Event, write_event


class Frame(C.Structure):
    _fields_ = [('type', C.c_char * 97), ('data_length', C.c_size_t),
                ('payload_length', C.c_size_t)]


class InteropTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('session_harness',
                                                     ROOT / 'scripts/test_esp32_session.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.harness = module.SessionTests()
        self.addCleanup(self.harness.doCleanups)
        self.lib = self.harness.library()
        self.IO = module.IO
        self.lib.wy_parse_header.argtypes = [C.c_char_p, C.c_size_t, C.POINTER(Frame)]
        self.lib.wy_parse_header.restype = C.c_int
        SatelliteClient.PLAYBACK_LOCK = asyncio.Lock()

    async def exchange(self, sizes):
        events = []
        pcm_chunks = [bytes((i % 251 for i in range(size))) for size in sizes]

        async def send(event):
            events.append(event)

        sleep = AsyncMock()
        fixtures = [(pcm, *SND_FORMAT) for pcm in pcm_chunks]
        with patch.object(tts, 'synthesize_chunks', return_value=iter(fixtures)), \
                patch.object(asyncio, 'sleep', sleep):
            async with tts.TtsStream(send, pace=2.0) as stream:
                seconds = await stream.say('fixture')

        self.assertEqual(events[0].type, 'audio-start')
        self.assertEqual(events[-1].type, 'audio-stop')
        self.assertEqual(sum(e.type == 'audio-start' for e in events), 1)
        self.assertEqual(sum(e.type == 'audio-stop' for e in events), 1)
        chunks = [e for e in events if e.type == 'audio-chunk']
        self.assertEqual(b''.join(e.payload for e in chunks), b''.join(pcm_chunks))
        self.assertTrue(all(len(e.payload) % (SND_FORMAT[1] * SND_FORMAT[2]) == 0
                            for e in chunks))
        duration = sum(sizes) / (SND_FORMAT[0] * SND_FORMAT[1] * SND_FORMAT[2])
        self.assertAlmostEqual(seconds, duration)
        self.assertAlmostEqual(sum(call.args[0] for call in sleep.await_args_list), duration / 2)

        # The real canonical serializer feeds the actual production C header parser.
        for event in events:
            encoded = io.BytesIO()
            write_event(event, encoded)
            header = encoded.getvalue().split(b'\n', 1)[0]
            frame = Frame()
            rc = self.lib.wy_parse_header(header, len(header), C.byref(frame))
            self.assertEqual(rc, 0, f'{event.type}: payload={len(event.payload or b"")} rc={rc}')

        # Also drive the same frames through the full production session, not only its parser.
        source = io.BytesIO()
        for event in [Event(type='describe'),
                      Event(type='hermes.register.ok', data={'device_id': 'interop-fixture',
                                                             'protocol_version': 1}),
                      Event(type='run-satellite'), Event(type='transcript', data={'text': ''}),
                      *events]:
            write_event(event, source)
        source.seek(0)
        output = io.BytesIO()

        @self.IO
        def read(_ctx, ptr, size):
            raw = source.read(size)
            C.memmove(ptr, raw, len(raw))
            return len(raw)

        @self.IO
        def write(_ctx, ptr, size):
            output.write(C.string_at(ptr, size))
            return size

        session = C.create_string_buffer(self.lib.wy_session_size())
        self.assertEqual(self.lib.wy_session_init(session, b'interop-fixture', b'boot'), 0)
        ring = (C.c_int32 * 262144)()
        self.assertEqual(self.lib.wy_session_playback_configure(session, ring, C.sizeof(ring), 100), 0)
        for _ in range(4 + len(events)):
            self.assertEqual(self.lib.wy_session_step(session, read, write, None), 0)
        self.assertEqual(self.lib.wy_session_state(session), 3)  # still RUN_REQUESTED

    async def test_large_sentence_is_accepted_by_firmware(self):
        await self.exchange([20000])

    async def test_payload_boundaries_preserve_bytes_and_pacing(self):
        for size in [0, 2, 16382, 16384, 16386, 80000]:
            with self.subTest(size=size):
                await self.exchange([size])

    async def test_multiple_sentences_share_one_stream(self):
        await self.exchange([80000, 2, 20000])


if __name__ == '__main__':
    unittest.main()
