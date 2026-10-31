#!/usr/bin/env python3
"""A wake detection is the user's only way to be heard; it must never be thrown away.

Why this exists. The interruption watcher owns the satellite queue while a task is
running, and when a wake arrived it either steered the running task or -- finding
no speech within its capture window -- logged and `continue`d. The detection was
consumed and gone. Measured: a second "hey hermes" played the activation tone and
then nothing at all, because the first turn was still finishing and its watcher
ate the wake.

The tone sounding is what makes this so confusing from the room: the device very
clearly said "I heard you", and then the Gateway dropped it.

Run with the Gateway's own interpreter:

    ~/hermes-voice-satellite/.venv/bin/python -B scripts/test_wake_never_dropped.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gateway.loop import VoiceLoop  # noqa: E402


class HeldWakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_wake_with_no_speech_is_kept_for_the_next_turn(self):
        """Drive the real watcher through the branch that used to drop the wake."""
        loop = VoiceLoop.__new__(VoiceLoop)
        loop._held_wake = None
        loop.sat = AsyncMock()
        loop.sat.eof = False
        loop._end_capture = AsyncMock()

        class State:
            done = False

        state = State()
        calls = {'n': 0}

        async def next_event(_timeout):
            calls['n'] += 1
            if calls['n'] == 1:
                return type('E', (), {'type': 'detection'})()
            state.done = True   # let the watcher wind down instead of spinning
            return None

        loop.sat.next_event = next_event
        # The capture finds no speech: the exact branch that discarded the wake.
        loop.capture_utterance = AsyncMock(return_value=None)

        with patch('gateway.loop.Detection') as Detection:
            Detection.is_type.return_value = True
            Detection.from_event.return_value = type('D', (), {'name': 'hey-hermes'})()
            await asyncio.wait_for(loop._watch_for_interruption(state), timeout=3.0)

        self.assertEqual(loop._held_wake, 'hey-hermes',
                         'a wake with no speech must be held, not dropped')
        self.assertTrue(loop._end_capture.called)

    async def test_a_held_wake_is_returned_by_wait_for_wake(self):
        loop = VoiceLoop.__new__(VoiceLoop)
        loop._held_wake = 'hey-hermes'
        loop._speaking = False
        loop.self_trigger_count = 0
        loop.sat = AsyncMock()
        loop.sat.eof = False
        name = await asyncio.wait_for(loop.wait_for_wake(timeout=1.0), timeout=2.0)
        self.assertEqual(name, 'hey-hermes')
        self.assertIsNone(loop._held_wake, 'a held wake is consumed exactly once')

    async def test_a_wake_during_wait_for_played_is_not_dismissed_as_echo(self):
        """THE measured failure: after a reply the turn waits for the device's
        `played`, which this firmware never sends. Every wake inside that wait was
        logged as "treating as a self-trigger" and discarded -- so one conversation
        worked and the next two attempts were silently swallowed."""
        loop = VoiceLoop.__new__(VoiceLoop)
        loop._held_wake = None
        loop._speaking = False
        loop.self_trigger_count = 0
        loop.sat = AsyncMock()
        loop.sat.eof = False

        async def next_event(_timeout):
            return type('E', (), {'type': 'detection'})()

        loop.sat.next_event = next_event
        with patch('gateway.loop.Detection') as Detection:
            Detection.is_type.return_value = True
            Detection.from_event.return_value = type('D', (), {'name': 'hey-hermes'})()
            finished = await asyncio.wait_for(loop.wait_for_played(timeout=5.0),
                                              timeout=3.0)
        self.assertTrue(finished, 'a wake means the person is done waiting')
        self.assertEqual(loop._held_wake, 'hey-hermes')
        self.assertEqual(loop.self_trigger_count, 0,
                         'a real wake must not be counted as our own echo')

    async def test_wait_for_played_does_not_park_a_turn_for_minutes(self):
        """A device that never emits `played` must not stall the conversation."""
        loop = VoiceLoop.__new__(VoiceLoop)
        loop._held_wake = None
        loop.sat = AsyncMock()
        loop.sat.eof = True          # the link is gone: nothing will arrive
        import inspect
        default = inspect.signature(loop.wait_for_played).parameters['timeout'].default
        self.assertLessEqual(default, 30.0,
                             f'wait_for_played default timeout is {default}s; a turn '
                             'must not be parked waiting for an event that may never come')

    async def test_no_held_wake_still_uses_the_live_queue(self):
        loop = VoiceLoop.__new__(VoiceLoop)
        loop._held_wake = None
        loop._speaking = False
        loop._speaking_at = 0.0
        loop.self_trigger_count = 0
        loop.sat = AsyncMock()
        loop.sat.eof = False
        with patch('gateway.loop.Detection') as Detection:
            Detection.is_type.return_value = True
            Detection.from_event.return_value = type('D', (), {'name': 'hey-hermes'})()
            loop.sat.next_event = AsyncMock(return_value=type(
                'E', (), {'type': 'detection'})())
            name = await asyncio.wait_for(loop.wait_for_wake(timeout=1.0), timeout=2.0)
        self.assertEqual(name, 'hey-hermes')


if __name__ == '__main__':
    unittest.main()
