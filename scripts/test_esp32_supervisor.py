#!/usr/bin/env python3
"""Compile the gateway link supervisor and drive its whole lifecycle with a scripted transport.

This is the loopback harness for the persistent-session item. The supervisor is pure (no sockets,
no FreeRTOS, no mbedTLS), so a scripted transport and a fake clock stand in for the network and for
time, and the tests pin down the properties the one-shot registration path could not:

  * a registered link is long-lived -- it stays on one connection across a long idle gap and many
    control events. The bridge keeps an idle dialled link alive with TCP keepalive only and sends
    no application-level ping, so the supervisor must never treat an idle read as a loss;
  * run/pause events are pumped on that same connection;
  * when the transport actually ends (a clean EOF), the link is torn down, the supervisor backs
    off, and it re-dials into a fresh session on the next attempt;
  * backoff grows with consecutive failures, is capped, resets after a good registration, and a
    finite attempt budget ends cleanly instead of looping;
  * a bridge refusal is terminal.

The fake read models a blocking read: when it is asked for bytes that are not yet available it
advances the fake clock by the event's lead time and then delivers them, exactly what a socket read
does (block until a byte or the deadline). That is what makes an arbitrary idle gap representable
and lets a test assert the link survives it.
"""
import ctypes as C
import io
import tempfile
import unittest
from pathlib import Path
import subprocess
from wyoming.event import Event, write_event

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / 'firmware/esp32/components'
IO = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.c_size_t)
NOW = C.CFUNCTYPE(C.c_int64)
ESTAB = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p)
TEARDOWN = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p)
ONREG = C.CFUNCTYPE(C.c_int, C.c_void_p)


class GWLink(C.Structure):
    _fields_ = [("user", C.c_void_p), ("read_fn", IO), ("write_fn", IO)]


def encode(events):
    """Pre-encode a sequence of wyoming events into one byte buffer."""
    out = io.BytesIO()
    for event in events:
        write_event(event, out)
    return out.getvalue()


class FakeClock:
    def __init__(self):
        self.value = 0


class FakeTransport:
    """A scripted peer. The queue holds (lead_us, bytes): the read blocks for lead_us (advancing
    the clock) then delivers the bytes. An empty queue with eof=True is a clean end-of-link."""

    def __init__(self, eof=True):
        self.queue = []          # list of [lead_us, bytes]
        self.cur = b''
        self.eof = eof
        self.establish_ok = True
        self.establish_fail_first = 0
        self.establish_calls = 0
        self.teardown_calls = 0
        self.clock = FakeClock()
        # The read/write trampolines must outlive the C struct that stores them. Holding them as
        # transport members (and the transport is kept alive by the establish/teardown bindings)
        # keeps the ctypes function pointers valid for as long as the C side may call them.
        self.read_fn = IO(self._read)
        self.write_fn = IO(self._write)

    def add(self, lead_us, events):
        self.queue.append([lead_us, encode(events)])

    def _pop_next(self):
        if not self.queue:
            return None
        lead, data = self.queue.pop(0)
        self.clock.value += lead
        return data

    def establish(self, _user, link_ptr):
        self.establish_calls += 1
        link = C.cast(link_ptr, C.POINTER(GWLink)).contents
        if (not self.establish_ok) or self.establish_calls <= self.establish_fail_first:
            return -1
        link.user = C.c_void_p(0)
        link.read_fn = self.read_fn
        link.write_fn = self.write_fn
        return 0

    def teardown(self, _user, _link_ptr):
        self.teardown_calls += 1

    def _read(self, _user, target, length):
        while not self.cur:
            nxt = self._pop_next()
            if nxt is None:
                return 0 if self.eof else -1   # 0 = clean EOF, -1 = I/O error
            self.cur = nxt
        n = min(length, len(self.cur))
        C.memmove(target, self.cur[:n], n)
        self.cur = self.cur[n:]
        return n

    def _write(self, _user, target, length):
        return length


class SupervisorTests(unittest.TestCase):
    def library(self):
        self.assertTrue((COMP / 'gateway_supervisor/gateway_supervisor.c').exists(),
                        'supervisor core missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'supervisor.so'
        includes = [COMP / 'wyoming_wire', COMP / 'wyoming_json', COMP / 'wyoming_json/vendor/cjson',
                    COMP / 'wyoming_session', COMP / 'gateway_supervisor']
        sources = [COMP / 'wyoming_wire/wyoming_wire.c', COMP / 'wyoming_json/wyoming_json.c',
                   COMP / 'wyoming_json/vendor/cjson/cJSON.c', COMP / 'wyoming_session/wyoming_session.c',
                   COMP / 'gateway_supervisor/gateway_supervisor.c']
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        '-DCJSON_NESTING_LIMIT=16', *['-I' + str(p) for p in includes],
                        *map(str, sources), '-lm', '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.gateway_supervisor_size.restype = C.c_size_t
        lib.gateway_supervisor_init.argtypes = [C.c_void_p, NOW, ESTAB, TEARDOWN, C.c_void_p,
                                                C.c_char_p, C.c_char_p, C.c_int64, C.c_int64,
                                                C.c_int, C.c_char_p, C.c_size_t, C.c_void_p]
        lib.gateway_supervisor_step.argtypes = [C.c_void_p]
        lib.gateway_supervisor_delay_us.argtypes = [C.c_void_p]
        lib.gateway_supervisor_delay_us.restype = C.c_int64
        for name in ['gateway_supervisor_registered', 'gateway_supervisor_events',
                     'gateway_supervisor_attempts', 'gateway_supervisor_reconnects',
                     'gateway_supervisor_refused']:
            getattr(lib, name).argtypes = [C.c_void_p]
        lib.gateway_supervisor_last_backoff_us.argtypes = [C.c_void_p]
        lib.gateway_supervisor_last_backoff_us.restype = C.c_int64
        lib.gateway_supervisor_state.argtypes = [C.c_void_p]
        return lib

    def run_supervisor(self, lib, transport, base_us=1000, max_us=0, max_attempts=0, on_registered=None):
        now_fn = NOW(lambda: transport.clock.value)
        estab_fn = ESTAB(transport.establish)
        teardown_fn = TEARDOWN(transport.teardown)
        self._keepalive = [now_fn, estab_fn, teardown_fn]
        self._onreg_fn = None
        if on_registered is not None:
            self._onreg_fn = ONREG(on_registered)
            self._keepalive.append(self._onreg_fn)
        sup = C.create_string_buffer(lib.gateway_supervisor_size())
        rc = lib.gateway_supervisor_init(sup, now_fn, estab_fn, teardown_fn, None, b'control-fixture', b'boot-1',
                         base_us, max_us, max_attempts, None, 0, self._onreg_fn)
        self.assertEqual(rc, 0, 'supervisor init failed')
        return sup

    def drive(self, lib, sup, until):
        """Step until `until(result, lib, sup)` is true or a terminal result is returned. When the
        supervisor reports GW_WAIT, advance the fake clock by the requested delay (the model of the
        caller sleeping the backoff). Returns (last_result, steps)."""
        steps = 0
        result = 0
        while True:
            result = lib.gateway_supervisor_step(sup)
            steps += 1
            if result == -2 or result == -4:      # GW_FAILED / GW_REFUSED are terminal
                return result, steps
            if until(result, lib, sup):
                return result, steps
            if result == -1:                        # GW_WAIT: sleep the backoff
                transport = self._transport
                transport.clock.value += lib.gateway_supervisor_delay_us(sup)
            if steps > 100000:
                self.fail('supervisor did not converge (possible spin)')

    def test_registers_and_stays_across_long_idle(self):
        lib = self.library()
        transport = FakeTransport(eof=True)
        self._transport = transport
        # Registration, then a 600 s idle gap (the bridge is silent during it -- keepalive only),
        # then two more control events. The gap is real: it lives inside the read.
        transport.add(0, [Event(type='describe'),
                          Event(type='hermes.register.ok',
                                data={'device_id': 'control-fixture', 'protocol_version': 1})])
        transport.add(600 * 1000000, [Event(type='run-satellite')])
        transport.add(1000, [Event(type='pause-satellite')])
        sup = self.run_supervisor(lib, transport)

        result, _ = self.drive(lib, sup, lambda r, l, s: l.gateway_supervisor_events(s) >= 2)
        self.assertEqual(lib.gateway_supervisor_registered(sup), 1, 'should be registered')
        self.assertEqual(lib.gateway_supervisor_events(sup), 2, 'both control events pumped')
        self.assertEqual(lib.gateway_supervisor_attempts(sup), 1, 'one connection for the whole lifetime')
        self.assertEqual(lib.gateway_supervisor_reconnects(sup), 0, 'an idle link is not a loss')
        self.assertEqual(transport.establish_calls, 1, 'established exactly once')
        self.assertGreaterEqual(transport.clock.value, 600 * 1000000,
                                'the 600 s idle gap elapsed while the link stayed open')

    def test_run_pause_transitions_on_the_same_link(self):
        lib = self.library()
        transport = FakeTransport(eof=True)
        self._transport = transport
        transport.add(0, [Event(type='describe'),
                          Event(type='hermes.register.ok',
                                data={'device_id': 'control-fixture', 'protocol_version': 1}),
                          Event(type='run-satellite'),
                          Event(type='pause-satellite'),
                          Event(type='run-satellite')])
        sup = self.run_supervisor(lib, transport)
        self.drive(lib, sup, lambda r, l, s: l.gateway_supervisor_events(s) >= 3)
        self.assertEqual(lib.gateway_supervisor_events(sup), 3)
        self.assertEqual(lib.gateway_supervisor_attempts(sup), 1, 'run/pause all on one link')
        self.assertEqual(transport.establish_calls, 1)

    def test_link_loss_backs_off_and_reconnects(self):
        lib = self.library()
        transport = FakeTransport(eof=True)
        self._transport = transport
        # First connection: register, one event, then a clean end-of-link.
        transport.add(0, [Event(type='describe'),
                          Event(type='hermes.register.ok',
                                data={'device_id': 'control-fixture', 'protocol_version': 1}),
                          Event(type='run-satellite')])
        sup = self.run_supervisor(lib, transport, base_us=5000)

        # Drive until the link is lost. drive stops at the GW_WAIT step (before the clock is
        # advanced past the backoff), so the backoff is still pending when it returns.
        self.drive(lib, sup, lambda r, l, s: l.gateway_supervisor_registered(s) == 0 and l.gateway_supervisor_events(s) >= 1)
        self.assertEqual(lib.gateway_supervisor_events(sup), 1)
        self.assertEqual(lib.gateway_supervisor_registered(sup), 0)
        self.assertEqual(lib.gateway_supervisor_delay_us(sup), 5000, 'backoff is pending, not yet elapsed')
        self.assertEqual(lib.gateway_supervisor_last_backoff_us(sup), 5000)

        # Provide the second connection and let the backoff elapse.
        transport.add(0, [Event(type='describe'),
                          Event(type='hermes.register.ok',
                                data={'device_id': 'control-fixture', 'protocol_version': 1}),
                          Event(type='pause-satellite')])
        transport.clock.value += 5000
        self.drive(lib, sup, lambda r, l, s: l.gateway_supervisor_registered(s) == 1 and l.gateway_supervisor_events(s) >= 2)
        self.assertEqual(lib.gateway_supervisor_registered(sup), 1)
        self.assertEqual(lib.gateway_supervisor_reconnects(sup), 1, 'counted as a reconnect')
        self.assertEqual(transport.establish_calls, 2)
        self.assertGreaterEqual(transport.teardown_calls, 1)

    def test_backoff_grows_and_caps(self):
        lib = self.library()
        transport = FakeTransport(eof=True)
        self._transport = transport
        transport.establish_ok = False          # every establish fails
        # max_attempts=5 so the fourth failure still schedules its (capped) backoff and the
        # clean stop happens on the fifth, after the cap has been observed.
        sup = self.run_supervisor(lib, transport, base_us=1000, max_us=8000, max_attempts=5)
        seen = []
        for target in (1, 2, 3, 4):
            self.drive(lib, sup, lambda r, l, s, t=target: l.gateway_supervisor_attempts(s) >= t)
            seen.append(lib.gateway_supervisor_last_backoff_us(sup))
        # base 1000, doubling: 1000, 2000, 4000, then capped at 8000.
        self.assertEqual(seen, [1000, 2000, 4000, 8000])
        self.assertEqual(lib.gateway_supervisor_attempts(sup), 4)
        # With a finite budget, once the final backoff elapses the supervisor stops cleanly.
        transport.clock.value += 8000
        self.assertEqual(lib.gateway_supervisor_step(sup), 0, 'final backoff elapses to IDLE')
        self.assertEqual(lib.gateway_supervisor_step(sup), -2, 'budget exhausted -> GW_FAILED, not a loop')

    def test_refusal_is_terminal(self):
        lib = self.library()
        transport = FakeTransport(eof=True)
        self._transport = transport
        transport.add(0, [Event(type='describe'),
                          Event(type='hermes.register.refused', data={'reason': 'no slot'})])
        sup = self.run_supervisor(lib, transport)
        result, _ = self.drive(lib, sup, lambda r, l, s: True)
        self.assertEqual(result, -4, 'refusal is terminal (GW_REFUSED)')
        self.assertEqual(lib.gateway_supervisor_refused(sup), 1)
        self.assertEqual(transport.establish_calls, 1)

    def test_establish_retries_then_succeeds(self):
        lib = self.library()
        transport = FakeTransport(eof=True)
        self._transport = transport
        transport.establish_fail_first = 2      # first two establishes fail, third works
        transport.add(0, [Event(type='describe'),
                          Event(type='hermes.register.ok',
                                data={'device_id': 'control-fixture', 'protocol_version': 1})])
        sup = self.run_supervisor(lib, transport, base_us=1000)
        self.drive(lib, sup, lambda r, l, s: l.gateway_supervisor_registered(s) == 1)
        self.assertEqual(lib.gateway_supervisor_registered(sup), 1)
        self.assertEqual(transport.establish_calls, 3, 'two failures then a success')
        self.assertEqual(lib.gateway_supervisor_attempts(sup), 3)
        self.assertEqual(lib.gateway_supervisor_reconnects(sup), 0, 'first-ever registration is not a reconnect')

    def test_on_registered_hook_fires_once_per_registration(self):
        """The device uses this hook to lift the link from its finite registration deadline to
        'no deadline' -- so it must fire exactly when a session (re)reaches registered, and never
        on the pump that follows. A hook that fired early (before registered) would let an idle
        read outlive a stalled handshake; one that fired on every pump would be noise, and one
        that missed a reconnect would leave a re-established link on its finite budget forever."""
        lib = self.library()
        transport = FakeTransport(eof=True)
        self._transport = transport
        # First connection: register, one event, then a clean end-of-link (the queue runs out).
        transport.add(0, [Event(type='describe'),
                          Event(type='hermes.register.ok',
                                data={'device_id': 'control-fixture', 'protocol_version': 1}),
                          Event(type='run-satellite')])
        calls = [0]
        def on_registered(_user):
            calls[0] += 1
            return 0
        sup = self.run_supervisor(lib, transport, base_us=5000, on_registered=on_registered)
        self.drive(lib, sup, lambda r, l, s: l.gateway_supervisor_events(s) >= 1)
        self.assertEqual(calls[0], 1, 'the hook fires exactly once, at the first registration')
        self.assertEqual(lib.gateway_supervisor_registered(sup), 1)
        self.assertEqual(transport.establish_calls, 1)

        # The link goes away (EOF on an empty queue); back off and reconnect.
        self.drive(lib, sup, lambda r, l, s: l.gateway_supervisor_registered(s) == 0
                   and l.gateway_supervisor_events(s) >= 1)
        self.assertEqual(calls[0], 1, 'the pump and the link loss do not refire the hook')

        transport.add(0, [Event(type='describe'),
                          Event(type='hermes.register.ok',
                                data={'device_id': 'control-fixture', 'protocol_version': 1})])
        transport.clock.value += 5000
        self.drive(lib, sup, lambda r, l, s: l.gateway_supervisor_registered(s) == 1
                   and l.gateway_supervisor_reconnects(s) >= 1)
        self.assertEqual(calls[0], 2, 'the hook fires again on the re-registration')
        self.assertEqual(transport.establish_calls, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
