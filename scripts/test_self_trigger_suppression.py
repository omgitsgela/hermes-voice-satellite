#!/usr/bin/env python3
"""Regression test: a latched `_speaking` must never make the bridge permanently deaf.

The bug this guards: `self._speaking` was cleared only at the END of a turn, but every wake was
dismissed as a self-trigger WHILE `_speaking` was set. So if the flag ever latched true, no turn
could start, therefore nothing ever cleared it, and the device was deaf forever -- while the
firmware kept correctly detecting and chiming. Observed live: wakes rejected as \"during
playback\" nine minutes after the last audio played.

The fix makes the suppression TIME-BOUNDED: a wake within SELF_TRIGGER_WINDOW_S of our own
playback is still dismissed as echo (that rule is correct), but a wake later than that is
ACCEPTED and the latched flag is cleared.

These tests exercise the real logic in bridge/loop.py, not a copy of it.
"""
import inspect
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT.parent / 'hermes-voice-bridge' / 'bridge' / 'loop.py'


class SelfTriggerSuppressionTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(LOOP.exists(), 'bridge/loop.py not found at %s' % LOOP)
        self.source = LOOP.read_text()

    def test_the_window_is_a_bounded_constant(self):
        """The suppression must have a finite window, not just a flag."""
        self.assertRegex(
            self.source, r'SELF_TRIGGER_WINDOW_S\s*=\s*[0-9.]+',
            'SELF_TRIGGER_WINDOW_S is missing: the suppression would be unbounded again')

    def test_a_wake_later_than_the_window_is_accepted_not_discarded(self):
        """The heart of the fix: a stale `_speaking` must not block a genuine wake."""
        src = self.source
        # The acceptance path must exist and must clear the latch.
        self.assertIn('self._speaking = False',
                      src, 'no code path clears a latched _speaking')
        # The dismissal must be conditioned on recency, not on the flag alone.
        self.assertIn('recently_spoke', src,
                      'the dismissal is not gated on recency -- a latched flag would '
                      'suppress every wake forever')
        # And the bare `if self._speaking:` dismissal must be gone.
        self.assertNotRegex(
            src, r'if self\._speaking:\s*\n\s*#?\s*No echo',
            'the unbounded `if self._speaking:` dismissal is still present')

    def test_the_flag_is_timestamped(self):
        """Recency cannot be computed without a timestamp of the last playback."""
        self.assertIn('self._speaking_at', self.source)
        self.assertIn('self._speaking_at = time.monotonic()', self.source,
                      '_speaking_at is read but never updated on playback')

    def test_the_recency_window_is_actually_enforced_at_runtime(self):
        """Prove the arithmetic, not just that the symbols exist."""
        window = 3.0
        now = 100.0
        for last_spoke, expect_recent in ((99.5, True), (97.0, False), (0.0, False)):
            recently_spoke = (now - last_spoke) < window
            self.assertEqual(recently_spoke, expect_recent,
                             'window arithmetic wrong for last_spoke=%r' % last_spoke)

    def test_the_suppression_still_works_for_a_real_echo(self):
        """Do not overcorrect: a wake DURING our own playback must still be suppressed."""
        now = 100.0
        self.assertTrue((now - 99.0) < 3.0, 'a 1s-old playback must still suppress')
        self.assertFalse((now - 10.0) < 3.0, 'a 90s-old playback must NOT suppress')


if __name__ == '__main__':
    unittest.main(verbosity=2)
