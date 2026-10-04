#!/usr/bin/env python3
"""Offline tests for the PC-side USB gateway relay.

The relay is a dumb, bounded pipe: it puts a USB-attached satellite into its link mode and moves bytes
between that port and a gateway's registry listener. These tests drive it with a scripted device and a
socketpair standing in for the gateway, so the framing, the handshake and every stop condition are
exercised without a device or a network.

The scripted device answers the mode command rather than pre-emitting anything, because the relay flushes
the port before it sends that command: a fake that shouts first is testing a device that does not exist.
"""
import io
import os
import select
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

from wyoming.event import Event, read_event, write_event

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import usb_gateway_relay as relay  # noqa: E402

DESCRIBE_MARKERS = (b'"type": "describe"', b'"type":"describe"')


class FakeSerial:
    """A selectable, pipe-backed stand-in for the device's serial port."""

    def __init__(self):
        self.to_host_r, self.to_host_w = os.pipe()
        self.from_host_r, self.from_host_w = os.pipe()
        self.closed = False

    def fileno(self):
        return self.to_host_r

    @property
    def in_waiting(self):
        readable, _, _ = select.select([self.to_host_r], [], [], 0)
        return 4096 if readable else 0

    def read(self, size):
        try:
            return os.read(self.to_host_r, size)
        except BlockingIOError:
            return b''

    def write(self, data):
        return os.write(self.from_host_w, data)

    def reset_input_buffer(self):
        while self.in_waiting:
            os.read(self.to_host_r, 4096)

    def emit(self, data):
        os.write(self.to_host_w, data)

    def close(self):
        self.closed = True
        for fd in (self.to_host_r, self.to_host_w, self.from_host_r, self.from_host_w):
            try:
                os.close(fd)
            except OSError:
                pass


def frame_bytes(event):
    buffer = io.BytesIO()
    write_event(event, buffer)
    return buffer.getvalue()


def scripted_device(port, stop, *, ack=True, respond=True, info_name='ESP32 control probe',
                    device_id='esp32-test0001'):
    """Play the device: acknowledge the mode command, answer describe, register.

    The thread is handed the session's stop event and joined by the session teardown:
    a thread left running past its test would outlive the file descriptors it uses, and
    the next test's pipe could be given the same numbers.
    """
    record = {'received': bytearray(), 'pending': bytearray()}

    def run():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not stop.is_set():
            try:
                readable, _, _ = select.select([port.from_host_r], [], [], 0.05)
                if not readable:
                    continue
                chunk = os.read(port.from_host_r, 4096)
            except OSError:
                return  # the port was closed under us; that is the end of the script
            if not chunk:
                continue
            record['received'].extend(chunk)
            record['pending'].extend(chunk)
            if ack and relay.DEFAULT_MODE_COMMAND.strip() in record['pending']:
                record['pending'] = bytearray()
                port.emit(relay.DEFAULT_ACK + b'\n')
            if not respond:
                continue
            if any(marker in record['pending'] for marker in DESCRIBE_MARKERS):
                record['pending'] = bytearray()
                port.emit(frame_bytes(Event(type='info', data={'satellite': {'name': info_name}})))
                port.emit(frame_bytes(Event(type='hermes.register', data={
                    'device_id': device_id, 'boot_id': 'b' * 32, 'protocol_version': 1,
                    'name': info_name, 'area': '', 'firmware_version': 'control-probe-1',
                    'capabilities': []})))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return record, thread


class GatewayPeer:
    """Stand in for the bridge's registry listener at the far end of a socketpair."""

    def __init__(self, *, reply_ok=True):
        self.host_side, self.relay_side = socket.socketpair()
        self.reply_ok = reply_ok
        self.received = bytearray()
        self.seen = []
        self.thread = None

    def connect(self, endpoint):
        return self.relay_side

    def start(self, stop):
        def run():
            self.host_side.sendall(frame_bytes(Event(type='describe')))
            pending = bytearray()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not stop.is_set():
                readable, _, _ = select.select([self.host_side], [], [], 0.05)
                if not readable:
                    continue
                try:
                    chunk = self.host_side.recv(4096)
                except OSError:
                    return  # the test closed the peer; that is the end of the script
                if not chunk:
                    return
                self.received.extend(chunk)
                pending.extend(chunk)
                # A stream, not a sequence of messages: an event can straddle two reads.
                stream = io.BytesIO(bytes(pending))
                consumed = 0
                while True:
                    event = read_event(stream)
                    if event is None:
                        break
                    consumed = stream.tell()
                    self.seen.append(event.type)
                    if event.type == 'hermes.register':
                        if self.reply_ok:
                            self.host_side.sendall(frame_bytes(Event(type='hermes.register.ok', data={
                                'device_id': event.data['device_id'], 'protocol_version': 1,
                                'name': event.data.get('name', ''), 'area': ''})))
                        else:
                            self.host_side.sendall(frame_bytes(Event(
                                type='hermes.register.refused', data={'reason': 'no recognised credential'})))
                        return
                del pending[:consumed]

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


class FakeSession:
    def __init__(self, *, reply_ok=True, respond=True, ack=True, device_id='esp32-test0001', banner=b''):
        self.stop = threading.Event()
        self.port = FakeSerial()
        self.gateway = GatewayPeer(reply_ok=reply_ok)
        self.gateway.start(self.stop)
        self.device_id = device_id
        if banner:
            self.port.emit(banner)
        self.device, self.device_thread = scripted_device(
            self.port, self.stop, ack=ack, respond=respond, device_id=device_id)

    def close(self):
        """Stop both scripted threads before their descriptors are released.

        Closing first would hand these file descriptor numbers to the next test while a
        stale thread was still selecting on them, which silently corrupts that test.
        """
        self.stop.set()
        for thread in (getattr(self, 'device_thread', None), self.gateway.thread):
            if thread is not None:
                thread.join(timeout=2.0)
        self.gateway.host_side.close()
        try:
            self.gateway.relay_side.close()
        except OSError:
            pass
        self.port.close()
        for thread in (getattr(self, 'device_thread', None), self.gateway.thread):
            if thread is not None and thread.is_alive():
                raise AssertionError('a scripted thread outlived its test')


def run(session, **kwargs):
    options = dict(endpoint=('127.0.0.1', 10701), duration=1.5, stop_when_registered=True)
    options.update(kwargs)
    return relay.run_relay(lambda: session.port, session.gateway.connect, **options)


class RelayTests(unittest.TestCase):
    def test_endpoint_parsing(self):
        self.assertEqual(relay.parse_endpoint('192.0.2.10:10701'), ('192.0.2.10', 10701))
        self.assertEqual(relay.parse_endpoint('bridge.example'), ('bridge.example', 10701))
        self.assertEqual(relay.parse_endpoint('bridge.example:9999'), ('bridge.example', 9999))
        for bad in ('', 'host:not-a-port', 'host:0', 'host:70000', ':10701'):
            with self.subTest(endpoint=bad), self.assertRaises(ValueError):
                relay.parse_endpoint(bad)

    def test_happy_path_registers_and_reports_identity(self):
        session = FakeSession()
        self.addCleanup(session.close)
        outcome = run(session, expect_device_id='esp32-test0001')
        self.assertEqual(outcome.exit_code, relay.EXIT_REGISTERED, outcome.error)
        self.assertTrue(outcome.registered)
        self.assertFalse(outcome.refused)
        self.assertEqual(outcome.device_id, 'esp32-test0001')
        self.assertEqual(outcome.stopped_because, 'registered')
        self.assertEqual(outcome.frames_from_device.get('info'), 1)
        self.assertEqual(outcome.frames_from_device.get('hermes.register'), 1)
        self.assertEqual(outcome.frames_to_device.get('hermes.register.ok'), 1)
        self.assertIn('hermes.register', session.gateway.seen)
        self.assertGreater(outcome.bytes_from_device, 0)
        self.assertGreater(outcome.bytes_to_device, 0)

    def test_no_acknowledgment_stops_before_connecting_to_the_gateway(self):
        session = FakeSession(ack=False)
        self.addCleanup(session.close)
        connected = []

        def tracking(endpoint):
            connected.append(endpoint)
            return session.gateway.connect(endpoint)

        outcome = relay.run_relay(lambda: session.port, tracking,
                                  endpoint=('127.0.0.1', 10701), duration=1.0, ack_timeout=0.8)
        self.assertEqual(outcome.exit_code, relay.EXIT_DEVICE_LINK_FAILED)
        self.assertEqual(outcome.stopped_because, 'no mode acknowledgment')
        self.assertEqual(connected, [], 'the gateway must not be dialled before the device is in link mode')
        self.assertIn(b'START_WYOMING', bytes(session.device['received']))

    def test_refusal_is_reported_not_treated_as_success(self):
        session = FakeSession(reply_ok=False)
        self.addCleanup(session.close)
        outcome = run(session)
        self.assertEqual(outcome.exit_code, relay.EXIT_REFUSED)
        self.assertTrue(outcome.refused)
        self.assertFalse(outcome.registered)
        self.assertEqual(outcome.refused_reason, 'no recognised credential')

    def test_unreachable_gateway_is_reported(self):
        session = FakeSession()
        self.addCleanup(session.close)

        def refuse_connection(endpoint):
            raise OSError('connection refused')

        outcome = relay.run_relay(lambda: session.port, refuse_connection,
                                  endpoint=('127.0.0.1', 10701), duration=1.0)
        self.assertEqual(outcome.exit_code, relay.EXIT_NOT_REGISTERED)
        self.assertIn('gateway unreachable', outcome.stopped_because)
        self.assertEqual(outcome.error, '', 'an unreachable gateway is not a device fault')

    def test_a_gateway_that_dies_with_a_reset_is_not_a_device_fault(self):
        """A gateway that crashes mid-run answers with a RST, not a FIN. Over a real network
        socket the relay's recv (or a mid-forward sendall) then raises an OSError on the
        GATEWAY side: the run must exit not-registered (3), the same as the FIN death that
        already does, not device-link-failed (4) -- or an operator chasing a crashed bridge is
        told to reseat the USB cable. This is the crash-shaped case the relay's own pinned
        principle (an unreachable gateway is not a device fault) must cover. A socketpair RST
        would not do this: it surfaces as an EOF, so the peer must be a real network listener.
        """
        session = FakeSession()
        self.addCleanup(session.close)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.addCleanup(listener.close)
        done = threading.Event()

        def crash_gateway():
            # Play the gateway: describe, wait for the device's register frame to cross, then
            # die with a RST (linger on, timeout 0) instead of a clean FIN.
            conn = None
            try:
                conn, _ = listener.accept()
                conn.sendall(frame_bytes(Event(type='describe')))
                received = bytearray()
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([conn], [], [], 0.05)
                    if not readable:
                        continue
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    received.extend(chunk)
                    if b'hermes.register' in received:
                        break
            except OSError:
                pass
            if conn is not None:
                try:
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                    struct.pack('ii', 1, 0))
                    conn.close()
                except OSError:
                    pass
            done.set()

        peer = threading.Thread(target=crash_gateway, daemon=True)
        peer.start()
        outcome = relay.run_relay(lambda: session.port,
                                  lambda endpoint: socket.create_connection(
                                      ('127.0.0.1', port), timeout=5),
                                  endpoint=('127.0.0.1', port), duration=10.0)
        peer.join(timeout=5.0)
        self.assertFalse(peer.is_alive(), 'the scripted peer outlived the run')
        self.assertEqual(outcome.exit_code, relay.EXIT_NOT_REGISTERED,
                         outcome.error or outcome.stopped_because)
        self.assertFalse(outcome.registered)
        self.assertEqual(outcome.error, '',
                         'a reset from the gateway is a gateway-side death, not a device fault')

    def test_device_id_mismatch_is_reported(self):
        session = FakeSession(device_id='esp32-somethingelse')
        self.addCleanup(session.close)
        outcome = run(session, expect_device_id='esp32-test0001')
        self.assertEqual(outcome.exit_code, relay.EXIT_IDENTITY_MISMATCH)
        self.assertTrue(outcome.mismatch)
        self.assertEqual(outcome.device_id, 'esp32-somethingelse')

    def test_silent_device_and_gateway_stop_at_the_duration_bound(self):
        session = FakeSession(respond=False)
        self.addCleanup(session.close)
        started = time.monotonic()
        outcome = relay.run_relay(lambda: session.port, session.gateway.connect,
                                  endpoint=('127.0.0.1', 10701), duration=0.6)
        elapsed = time.monotonic() - started
        self.assertEqual(outcome.exit_code, relay.EXIT_NOT_REGISTERED)
        self.assertEqual(outcome.stopped_because, 'duration reached')
        self.assertLess(elapsed, 5.0, 'the relay must stop at its bound, not hang')

    def test_bytes_are_forwarded_unchanged_in_both_directions(self):
        session = FakeSession()
        self.addCleanup(session.close)
        outcome = run(session)
        # Upstream: the gateway received exactly the device's frames, nothing added or reordered.
        expected_upstream = (
            frame_bytes(Event(type='info', data={'satellite': {'name': 'ESP32 control probe'}}))
            + frame_bytes(Event(type='hermes.register', data={
                'device_id': 'esp32-test0001', 'boot_id': 'b' * 32, 'protocol_version': 1,
                'name': 'ESP32 control probe', 'area': '', 'firmware_version': 'control-probe-1',
                'capabilities': []})))
        self.assertEqual(bytes(session.gateway.received), expected_upstream)
        self.assertEqual(outcome.bytes_from_device, len(expected_upstream))
        # Downstream: the mode command, then the gateway's bytes with nothing invented in between.
        # The relay stops the instant the device registers, so whether the gateway's acknowledgment
        # had already been forwarded at that moment is a genuine race and both outcomes are correct;
        # what must hold either way is that the device saw the mode command and the describe, and
        # that nothing beyond the acknowledgment was ever written to it.
        describe = frame_bytes(Event(type='describe'))
        acknowledgment = frame_bytes(Event(type='hermes.register.ok', data={
            'device_id': 'esp32-test0001', 'protocol_version': 1,
            'name': 'ESP32 control probe', 'area': ''}))
        downstream = bytes(session.device['received'])
        prefix = relay.DEFAULT_MODE_COMMAND + describe
        self.assertTrue(downstream.startswith(prefix),
                        'the device must receive the mode command and the describe unchanged')
        remainder = downstream[len(prefix):]
        self.assertIn(remainder, (b'', acknowledgment),
                      'only the gateway\'s own frame may follow, with nothing reordered or invented')
        # The relay closes the port the moment it stops, so bytes it had written but the device
        # had not yet read are legitimately discarded: the two counts cannot be asserted equal.
        # What must hold is that the relay never claims to have written more than the gateway sent.
        self.assertGreaterEqual(outcome.bytes_to_device, len(describe))
        self.assertLessEqual(outcome.bytes_to_device, len(describe) + len(acknowledgment),
                             'the relay must not report more downstream bytes than it was given')

    def test_ready_marker_is_consumed_locally_and_not_forwarded(self):
        session = FakeSession(banner=b'PROVISION READY 1 NETWORK_INACTIVE AUDIO_INACTIVE\n')
        self.addCleanup(session.close)
        outcome = run(session, ready_marker='PROVISION READY')
        self.assertEqual(outcome.exit_code, relay.EXIT_REGISTERED, outcome.error)
        self.assertNotIn(b'PROVISION READY', bytes(session.gateway.received),
                         'the device banner is host bookkeeping, not protocol traffic')
        self.assertIn(b'START_WYOMING', bytes(session.device['received']))

    def test_missing_ready_marker_stops_without_sending_the_mode_command(self):
        session = FakeSession()
        self.addCleanup(session.close)
        outcome = run(session, ready_marker='PROVISION READY', duration=1.0, ready_timeout=0.5)
        self.assertEqual(outcome.exit_code, relay.EXIT_DEVICE_LINK_FAILED)
        self.assertEqual(outcome.stopped_because, 'no ready marker')
        self.assertNotIn(b'START_WYOMING', bytes(session.device['received']),
                         'the mode command must not be sent to a device that is still booting')


if __name__ == '__main__':
    unittest.main()
