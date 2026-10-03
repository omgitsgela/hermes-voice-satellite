#!/usr/bin/env python3
"""Host tests for the shared serial-console helpers: port resolution and the bounded reader.

No pyserial and no device required: a fake connection stands in for the USB port. The point of the
shared module is public-safety -- the serial port comes from an argument or the environment, never
from a constant in the tree -- so the tests pin that a missing port is a refusal, not a guess, and
that the reader ends cleanly on a needle, a deadline, or a dead port."""
from pathlib import Path
import importlib.util
import os
import unittest


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeConnection:
    """Serves canned chunks one read at a time, and can die part-way through the stream."""

    def __init__(self, chunks, die_after=None, die_error=None):
        self.chunks = list(chunks)
        self.die_after = die_after
        self.die_error = die_error if die_error is not None else OSError('port went away')
        self.reads = 0

    def read(self, length):
        self.reads += 1
        if self.die_after is not None and self.reads > self.die_after:
            raise self.die_error
        if not self.chunks:
            return b''
        return self.chunks.pop(0)


class ResolvePortTests(unittest.TestCase):
    def setUp(self):
        self.module = _load('serial_console')
        self._old = os.environ.get('HERMES_SATELLITE_SERIAL')

    def tearDown(self):
        if self._old is None:
            os.environ.pop('HERMES_SATELLITE_SERIAL', None)
        else:
            os.environ['HERMES_SATELLITE_SERIAL'] = self._old

    def test_explicit_argument_wins_over_environment(self):
        os.environ['HERMES_SATELLITE_SERIAL'] = '/dev/env-port'
        self.assertEqual(self.module.resolve_port('/dev/arg-port'), '/dev/arg-port')

    def test_environment_is_used_when_no_argument_is_given(self):
        os.environ['HERMES_SATELLITE_SERIAL'] = '/dev/env-port'
        self.assertEqual(self.module.resolve_port(None), '/dev/env-port')
        self.assertEqual(self.module.resolve_port(), '/dev/env-port')

    def test_no_source_is_a_clear_refusal_not_a_hardcoded_default(self):
        os.environ.pop('HERMES_SATELLITE_SERIAL', None)
        with self.assertRaises(SystemExit) as ctx:
            self.module.resolve_port(None)
        self.assertIn('HERMES_SATELLITE_SERIAL', str(ctx.exception))
        self.assertIn('--port', str(ctx.exception))


class WaitForTests(unittest.TestCase):
    def setUp(self):
        self.module = _load('serial_console')

    def test_returns_the_stream_and_found_when_the_needle_arrives(self):
        conn = FakeConnection([b'PROVISION READY 1 ', b'NETWORK_INACTIVE\nrest'])
        received, found = self.module.wait_for(conn, b'PROVISION READY', 1.0)
        self.assertTrue(found)
        self.assertIn(b'PROVISION READY 1', received)
        self.assertEqual(conn.reads, 1, 'a found needle stops the reader; the rest is not consumed')

    def test_deadline_exhaustion_reports_not_found_with_what_arrived(self):
        conn = FakeConnection([b'partial '])
        received, found = self.module.wait_for(conn, b'NEVER', 0.05)
        self.assertFalse(found)
        self.assertEqual(bytes(received), b'partial ')

    def test_a_read_error_is_a_ended_stream_not_a_crash(self):
        conn = FakeConnection([b'PROVISION READY '], die_after=1)
        received, found = self.module.wait_for(conn, b'NEVER', 1.0)
        self.assertFalse(found)
        self.assertIn(b'PROVISION READY', received)

    def test_port_gone_error_type_is_handled_the_same_way(self):
        # A pyserial disconnect surfaces as a SerialException, which subclasses OSError: it must
        # end the stream as not-found rather than escape, exactly like a plain OSError does.
        class SerialLike(OSError):
            pass
        conn = FakeConnection([b'x'], die_after=1, die_error=SerialLike('gone'))
        received, found = self.module.wait_for(conn, b'NEVER', 1.0)
        self.assertFalse(found)
        self.assertEqual(bytes(received), b'x')

    def test_echo_sink_gets_the_stream_but_is_optional(self):
        # The sink receives decoded text, the same way the run scripts write to stdout.
        sink = []

        class Sink:
            def write(self, data):
                sink.append(data)
                return len(data)

            def flush(self):
                pass

        conn = FakeConnection([b'PROVISION READY\n'])
        received, found = self.module.wait_for(conn, b'PROVISION READY', 1.0, echo=Sink())
        self.assertTrue(found)
        self.assertEqual(''.join(sink), 'PROVISION READY\n')
        self.assertEqual(bytes(received), b'PROVISION READY\n')


if __name__ == '__main__':
    unittest.main()
