#!/usr/bin/env python3
"""Host tests for scripts/soak_runner: the thin glue between a serial connection and the
soak verdict.

The verdict logic itself is soak_harness and is tested in test_esp32_soak.py. What this
glue adds is the polling: send GATEWAY_STATUS, read the one PROVISION line that answers,
turn a silent or dead port into a 'lost' sample rather than a crash (a flapping USB link
is exactly what a soak is for, and it must be recorded, not fatal), and repeat until the
window is over. Everything the glue touches that is not pure -- time and the port -- is
injected, so the whole loop runs on a host with no board and no pyserial.
"""
import importlib
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))


def load():
    return importlib.import_module('soak_runner')


class FakeClock:
    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeConnection:
    """Scripted reads for a poll. Each script entry is read back as ONE read() call:
    a bytes entry is emitted in full (a line that has already left the chip before the
    command was written -- boot noise -- or the answer), None is a quiet read (and
    advances the fake clock, the way a real port's silence is where real time goes), and
    a list of bytes entries is a multi-line chunk (the answer's own PROVISION line
    followed by the next burst). The connection never reads ahead of its script, so the
    test script IS the device's output order."""

    def __init__(self, script, clock, step=0.5):
        self.script = list(script)
        self.clock = clock
        self.step = step
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def read(self, n=512):
        if not self.script:
            self.clock.advance(self.step)
            return b''
        entry = self.script.pop(0)
        if entry is None:
            self.clock.advance(self.step)
            return b''
        if isinstance(entry, list):
            return b''.join(entry)
        return entry


RUNNING = b'PROVISION OK GATEWAY RUNNING last=session=registered\n'
IDLE = b'PROVISION OK GATEWAY IDLE\n'


class SampleStatusTest(unittest.TestCase):
    def test_one_poll_returns_the_parsed_status(self):
        runner = load()
        clock = FakeClock()
        connection = FakeConnection([RUNNING], clock)
        kind, last = runner.sample_status(connection, clock(), 5.0, clock)
        self.assertEqual((kind, last), ('running', 'session=registered'))
        self.assertEqual(connection.writes, [b'GATEWAY_STATUS\n'])

    def test_a_quiet_port_times_out_as_lost_not_crash(self):
        runner = load()
        clock = FakeClock()
        connection = FakeConnection([None], clock, step=0.5)
        kind, last = runner.sample_status(connection, clock(), 1.0, clock)
        self.assertEqual((kind, last), ('lost', ''))
        self.assertGreaterEqual(clock(), 1.0, 'the sample budget must have been spent')

    def test_boot_noise_does_not_answer_the_poll(self):
        # The console prints its boot lines before READY; a line that is not a GATEWAY
        # answer -- even one with the PROVISION prefix, like the banner -- is skipped, and
        # the poll keeps waiting for the real answer.
        runner = load()
        clock = FakeClock()
        connection = FakeConnection([b'PROVISION READY 1 NETWORK_INACTIVE AUDIO_INACTIVE\n'
                                    + RUNNING], clock)
        kind, last = runner.sample_status(connection, clock(), 5.0, clock)
        self.assertEqual((kind, last), ('running', 'session=registered'))

    def test_an_unrecognised_provision_line_is_unexpected(self):
        runner = load()
        clock = FakeClock()
        connection = FakeConnection([b'PROVISION OK GATEWAY RUNNING\n'], clock)
        kind, last = runner.sample_status(connection, clock(), 5.0, clock)
        self.assertEqual(kind, 'unexpected')


class RunSoakTest(unittest.TestCase):
    def _run(self, replies, duration, interval=1.0, step=0.5, sample_timeout=1.0):
        runner = load()
        clock = FakeClock()

        def sleep(dt):
            clock.advance(dt)

        connection = FakeConnection(replies, clock, step=step)
        verdict = runner.run_soak(connection, duration, interval, clock, sleep, sample_timeout)
        return runner, verdict, clock, connection

    def test_a_window_of_registered_samples_is_ok(self):
        runner, verdict, clock, connection = self._run([RUNNING] * 11, duration=10.0)
        self.assertTrue(verdict['ok'], verdict)
        self.assertEqual(verdict['samples'], 11)
        self.assertEqual(verdict['running'], 11)
        self.assertEqual(verdict['registered'], 11)
        # One GATEWAY_STATUS per sample, nothing else on the wire.
        self.assertEqual(len(connection.writes), 11)
        self.assertEqual(connection.writes[0], b'GATEWAY_STATUS\n')

    def test_silence_is_recorded_as_lost_samples_and_fails(self):
        runner, verdict, clock, connection = self._run([None, None], duration=4.0,
                                                       sample_timeout=1.0)
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['reason'], 'lost_at_sample_0')
        self.assertEqual(verdict['lost'], verdict['samples'])
        self.assertGreaterEqual(verdict['samples'], 1)

    def test_an_idle_sample_fails_the_soak(self):
        runner, verdict, _clock, _connection = self._run([RUNNING, IDLE, RUNNING], duration=4.0)
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['reason'], 'idle_at_sample_1')

    def test_exit_code_tracks_the_verdict(self):
        runner, ok_verdict, _c, _x = self._run([RUNNING] * 3, duration=2.0)
        self.assertTrue(ok_verdict['ok'], ok_verdict)
        self.assertEqual(runner.soak_exit(ok_verdict), 0)
        _runner, bad_verdict, _c2, _x2 = self._run([IDLE], duration=2.0)
        self.assertFalse(bad_verdict['ok'])
        self.assertEqual(runner.soak_exit(bad_verdict), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
