#!/usr/bin/env python3
"""Silent, offline TTS stream lifecycle checks. No synthesis, sockets, or audio devices."""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from satellite_gateway import tts
from satellite_gateway.config import SND_FORMAT
from satellite_gateway.satellite import SatelliteClient


class FailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        SatelliteClient.PLAYBACK_LOCK = asyncio.Lock()

    async def test_stop_failure_preserves_the_original_failure(self):
        primary = ValueError('original synthesis failure')

        async def send(event):
            if event.type == 'audio-stop':
                raise ConnectionError('cleanup connection failure')

        with self.assertRaises(ValueError) as raised:
            async with tts.TtsStream(send):
                raise primary
        self.assertIs(raised.exception, primary)
        self.assertFalse(SatelliteClient.PLAYBACK_LOCK.locked())

    async def test_stop_failure_on_a_successful_body_is_not_hidden(self):
        async def send(event):
            if event.type == 'audio-stop':
                raise ConnectionError('cleanup connection failure')

        with self.assertRaises(ConnectionError):
            async with tts.TtsStream(send):
                pass
        self.assertFalse(SatelliteClient.PLAYBACK_LOCK.locked())

    async def test_body_cancellation_is_not_replaced_by_cleanup_failure(self):
        async def send(event):
            if event.type == 'audio-stop':
                raise ConnectionError('cleanup connection failure')

        with self.assertRaises(asyncio.CancelledError):
            async with tts.TtsStream(send):
                raise asyncio.CancelledError()
        self.assertFalse(SatelliteClient.PLAYBACK_LOCK.locked())

    async def test_failed_second_chunk_keeps_first_chunk_accounting(self):
        events = []

        async def send(event):
            events.append(event)
            if sum(e.type == 'audio-chunk' for e in events) == 2:
                raise ConnectionError('second chunk failed')

        stream = tts.TtsStream(send, pace=0)
        fixture = [(bytes(10000), *SND_FORMAT)]
        with patch.object(tts, 'synthesize_chunks', return_value=iter(fixture)):
            with self.assertRaises(ConnectionError):
                async with stream:
                    await stream.say('fixture')
        chunks = [e for e in events if e.type == 'audio-chunk']
        self.assertEqual(len(chunks), 2, 'no automatic retransmission after an I/O failure')
        duration = 4096 / (SND_FORMAT[0] * SND_FORMAT[1] * SND_FORMAT[2])
        self.assertAlmostEqual(stream.seconds_sent, duration)
        self.assertFalse(SatelliteClient.PLAYBACK_LOCK.locked())


if __name__ == '__main__':
    unittest.main()
