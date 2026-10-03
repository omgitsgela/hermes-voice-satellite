#!/usr/bin/env python3
"""Host tests for scripts/soak_harness: the pure core of the soak and telemetry harness.

A soak of a satellite does one thing: poll the read-only GATEWAY_STATUS console command
while the device is running, collect the samples, and answer "did the registered link
stay alive for the whole window?" The device-side half (the GATEWAY_STATUS command and
the supervisor's report) is proved elsewhere; this core is the decision, and it is pure
-- no serial, no timers -- so the verdict can be driven by scripted samples on a host.

The honest definition of healthy, from the supervisor's contract: the persistent task is
active the whole window (every sample is RUNNING), and the link actually registered at
least once. A sample whose last line is a backoff or a failed attempt is NOT a failure:
a dropped link that the supervisor reconnects is exactly the property item 1 exists to
prove. What IS a failure: an IDLE sample (the task never ran or terminated, e.g. a
refused registration), a LOST sample (the console stopped answering), an UNEXPECTED
sample (the console changed its words), or a window in which no sample ever shows the
link registered.
"""
import importlib
import unittest


def load():
    import sys
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'scripts'))
    return importlib.import_module('soak_harness')


class ParseStatusLineTest(unittest.TestCase):
    def setUp(self):
        self.soak = load()

    def test_idle_line(self):
        self.assertEqual(self.soak.parse_status_line('OK GATEWAY IDLE'), ('idle', ''))

    def test_running_with_a_last_line(self):
        self.assertEqual(
            self.soak.parse_status_line('OK GATEWAY RUNNING last=session=registered'),
            ('running', 'session=registered'))

    def test_running_preserves_spaces_inside_the_last_line(self):
        self.assertEqual(
            self.soak.parse_status_line('OK GATEWAY RUNNING last=reconnect=registered reconnects=2'),
            ('running', 'reconnect=registered reconnects=2'))

    def test_running_with_an_empty_report_is_running(self):
        # The task is active but the supervisor has not appended a line yet (still dialing).
        self.assertEqual(self.soak.parse_status_line('OK GATEWAY RUNNING last='), ('running', ''))

    def test_an_unrecognised_line_is_not_a_guess(self):
        # A console that changes its words is protocol drift, not a link problem; it must be
        # reported as unexpected rather than silently read as healthy or as a drop.
        self.assertEqual(self.soak.parse_status_line('OK GATEWAY RUNNING'),
                         ('unexpected', 'OK GATEWAY RUNNING'))
        self.assertEqual(self.soak.parse_status_line('OK UNCONFIGURED'),
                         ('unexpected', 'OK UNCONFIGURED'))
        self.assertEqual(self.soak.parse_status_line(''), ('unexpected', ''))


class SoakVerdictTest(unittest.TestCase):
    def setUp(self):
        self.soak = load()

    def samples(self, *kinds_lasts):
        """Build (elapsed, kind, last) samples, one second apart, starting at t=0."""
        return [(float(i), kind, last) for i, (kind, last) in enumerate(kinds_lasts)]

    def test_a_window_of_registered_samples_is_ok(self):
        verdict = self.soak.soak_verdict(self.samples(
            ('running', 'session=registered'),
            ('running', 'session=registered'),
            ('running', 'session=registered')))
        self.assertTrue(verdict['ok'], verdict)
        self.assertEqual(verdict['reason'], 'ok')
        self.assertEqual(verdict['samples'], 3)
        self.assertEqual(verdict['running'], 3)
        self.assertEqual(verdict['registered'], 3)
        self.assertIsNone(verdict['first_problem'])

    def test_backoff_and_reconnect_samples_are_not_failures(self):
        # A link that dropped and came back is the property under test, not a defect.
        verdict = self.soak.soak_verdict(self.samples(
            ('running', 'session=registered'),
            ('running', 'backoff=2000000 us'),
            ('running', 'reconnect=registered reconnects=1'),
            ('running', 'reconnect=registered reconnects=2')))
        self.assertTrue(verdict['ok'], verdict)
        self.assertEqual(verdict['backoffs'], 1)
        self.assertEqual(verdict['max_reconnects'], 2)
        self.assertEqual(verdict['registered'], 3)

    def test_an_idle_sample_fails_with_the_first_offender(self):
        verdict = self.soak.soak_verdict(self.samples(
            ('running', 'session=registered'),
            ('idle', ''),
            ('idle', '')))
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['first_problem'], (1, 'idle', ''))
        self.assertIn('sample_1', verdict['reason'])

    def test_a_lost_sample_fails(self):
        verdict = self.soak.soak_verdict(self.samples(
            ('running', 'session=registered'),
            ('lost', '')))
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['first_problem'], (1, 'lost', ''))

    def test_an_unexpected_sample_fails(self):
        verdict = self.soak.soak_verdict(self.samples(
            ('running', 'session=registered'),
            ('unexpected', 'OK GATEWAY RUNNING')))
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['first_problem'], (1, 'unexpected', 'OK GATEWAY RUNNING'))

    def test_never_registered_is_a_failure_even_when_the_task_runs(self):
        # The task is alive and dialing, but the link never came up in the window.
        verdict = self.soak.soak_verdict(self.samples(
            ('running', ''),
            ('running', 'establish=failed attempt=1'),
            ('running', 'backoff=2000000 us')))
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['reason'], 'never_registered')
        self.assertIsNone(verdict['first_problem'])
        self.assertEqual(verdict['registered'], 0)

    def test_a_problem_outranks_never_registered(self):
        # The task dying is the more direct problem; report it, not the registration gap.
        verdict = self.soak.soak_verdict(self.samples(
            ('running', 'backoff=2000000 us'),
            ('idle', '')))
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['reason'], 'idle_at_sample_1')
        self.assertEqual(verdict['first_problem'], (1, 'idle', ''))

    def test_an_empty_window_proves_nothing(self):
        verdict = self.soak.soak_verdict([])
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['reason'], 'no_samples')
        self.assertEqual(verdict['samples'], 0)

    def test_an_empty_last_line_is_not_registered(self):
        verdict = self.soak.soak_verdict(self.samples(
            ('running', ''),
            ('running', 'session=registered')))
        self.assertTrue(verdict['ok'])
        self.assertEqual(verdict['registered'], 1, 'only the line that says registered counts')


class RenderReportTest(unittest.TestCase):
    def setUp(self):
        self.soak = load()

    def test_ok_report_names_the_window(self):
        verdict = self.soak.soak_verdict([(0.0, 'running', 'session=registered'),
                                          (1.0, 'running', 'session=registered')])
        line = self.soak.render_report(verdict)
        self.assertTrue(line.startswith('SOAK OK'), line)
        self.assertIn('samples=2', line)
        self.assertIn('running=2', line)
        self.assertIn('registered=2', line)

    def test_a_failed_report_names_the_reason_and_the_first_offender(self):
        verdict = self.soak.soak_verdict([(0.0, 'running', 'session=registered'),
                                          (1.0, 'idle', '')])
        line = self.soak.render_report(verdict)
        self.assertTrue(line.startswith('SOAK FAIL'), line)
        self.assertIn('idle_at_sample_1', line)

    def test_the_report_is_one_line(self):
        verdict = self.soak.soak_verdict([(0.0, 'idle', '')])
        self.assertNotIn('\n', self.soak.render_report(verdict))


if __name__ == '__main__':
    unittest.main(verbosity=2)
