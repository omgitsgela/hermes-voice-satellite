#!/usr/bin/env python3
"""Bridge a USB-attached satellite to a gateway's registry listener from a PC.

An embedded satellite has one USB port and no network of its own yet. This is the ordinary way to
reach a bridge with it: the PC owns the serial port, puts the device into its link mode, and becomes a
transparent pipe to the gateway. It is the same shape as the device-side runner in
`pi/outbound_satellite.py`, with the relay leg pointed at a serial port instead of a local satellite.

What it is not: it never speaks the protocol on its own. It sends the one mode command, then moves
bytes. It does not answer `describe`, does not invent a `pong`, and does not relay anything that did not
come from one of the two ends.

Boundaries, because a pipe that runs unattended must have them:

* the device must acknowledge the mode command before the gateway is dialled at all, so a half-booted
  device never shows up as a registered-but-silent satellite;
* a run has a duration bound and stops on either end closing;
* a device banner (`PROVISION READY ...`) is consumed as host bookkeeping and never forwarded;
* nothing is written to the device after the mode command.

Exit codes say what happened, so a caller does not have to parse prose:

    0 registered       the gateway accepted this device
    2 refused          the gateway refused it; retrying will not help until a human acts
    3 not registered   no acceptance inside the run (gateway silent, unreachable, or closed)
    4 device link failed  the port, the banner or the mode acknowledgment was wrong
    5 identity mismatch   the device registered under a different id than expected
"""

from __future__ import annotations

import argparse
import json
import logging
import select
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

DEFAULT_MODE_COMMAND = b'START_WYOMING\n'
DEFAULT_ACK = b'PROVISION OK WYOMING CONTROL_ONLY RESET_REQUIRED'
DEFAULT_GATEWAY_PORT = 10701
#: Mirror the project's one keepalive implementation (see bridge/net.py). An idle link has no timeout of
#: its own, and a relay that outlives a vanished gateway would hold a socket that looks alive forever.
KEEPALIVE_IDLE_SECONDS = 15
KEEPALIVE_INTERVAL_SECONDS = 5
KEEPALIVE_PROBES = 3

EXIT_REGISTERED = 0
EXIT_REFUSED = 2
EXIT_NOT_REGISTERED = 3
EXIT_DEVICE_LINK_FAILED = 4
EXIT_IDENTITY_MISMATCH = 5

REGISTER_EVENT = 'hermes.register'
REGISTER_OK_EVENT = 'hermes.register.ok'
REGISTER_REFUSED_EVENT = 'hermes.register.refused'

log = logging.getLogger('usb-gateway-relay')


def parse_endpoint(text: str) -> Tuple[str, int]:
    """`host` or `host:port` into a (host, port) pair. Bad input raises ValueError, never guesses."""
    text = (text or '').strip()
    if not text:
        raise ValueError('empty gateway endpoint')
    host, separator, port_text = text.rpartition(':')
    if not separator:
        return text, DEFAULT_GATEWAY_PORT
    if not host:
        raise ValueError(f'no host in endpoint {text!r}')
    try:
        port = int(port_text)
    except ValueError as error:
        raise ValueError(f'port is not a number in {text!r}') from error
    if not 1 <= port <= 65535:
        raise ValueError(f'port out of range in {text!r}')
    return host, port


@dataclass
class RelayOutcome:
    registered: bool = False
    refused: bool = False
    mismatch: bool = False
    device_id: Optional[str] = None
    refused_reason: str = ''
    frames_from_device: Dict[str, int] = field(default_factory=dict)
    frames_to_device: Dict[str, int] = field(default_factory=dict)
    bytes_from_device: int = 0
    bytes_to_device: int = 0
    stopped_because: str = ''
    error: str = ''

    @property
    def exit_code(self) -> int:
        if self.error:
            return EXIT_DEVICE_LINK_FAILED
        if self.refused:
            return EXIT_REFUSED
        if not self.registered:
            return EXIT_NOT_REGISTERED
        if self.mismatch:
            return EXIT_IDENTITY_MISMATCH
        return EXIT_REGISTERED


class FrameObserver:
    """Count the events crossing the link, without ever altering the stream.

    This is diagnostics, not protocol: the relay's job is to move bytes. Anything it cannot parse is
    counted as unparsed and skipped, and the running byte budget is what the summary reports.
    """

    MAX_HEADER = 4096
    MAX_BODY = 1 << 20

    def __init__(self) -> None:
        self.pending = bytearray()
        self.counts: Dict[str, int] = {}
        self.unparsed = 0
        self.data: Dict[str, Dict] = {}

    def feed(self, chunk: bytes) -> None:
        self.pending.extend(chunk)
        while True:
            newline = self.pending.find(b'\n')
            if newline < 0:
                if len(self.pending) > self.MAX_HEADER:
                    self.unparsed += 1
                    self.pending.clear()
                return
            header = bytes(self.pending[:newline])
            body_needed = 0
            payload_needed = 0
            event_type = ''
            try:
                parsed = json.loads(header.decode('utf-8'))
                event_type = str(parsed['type'])
                # The Python library omits a zero length key entirely, while the device's own
                # serializer always writes both, so a missing length means zero, not malformed.
                body_needed = int(parsed.get('data_length', 0))
                payload_needed = int(parsed.get('payload_length', 0))
                if not 0 <= body_needed <= self.MAX_BODY or not 0 <= payload_needed <= self.MAX_BODY:
                    raise ValueError('length out of range')
            except (ValueError, KeyError, UnicodeDecodeError, TypeError, AttributeError):
                self.unparsed += 1
                del self.pending[:newline + 1]
                continue
            total = newline + 1 + body_needed + payload_needed
            if len(self.pending) < total:
                return
            body = bytes(self.pending[newline + 1:newline + 1 + body_needed])
            del self.pending[:total]
            self.counts[event_type] = self.counts.get(event_type, 0) + 1
            if body:
                try:
                    self.data[event_type] = json.loads(body.decode('utf-8'))
                except (ValueError, UnicodeDecodeError):
                    pass


def enable_tcp_keepalive(sock: socket.socket, label: str = 'gateway') -> bool:
    """Turn on kernel keepalive. Never fatal: a socket option must not stop a link coming up."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (('TCP_KEEPIDLE', KEEPALIVE_IDLE_SECONDS),
                            ('TCP_KEEPINTVL', KEEPALIVE_INTERVAL_SECONDS),
                            ('TCP_KEEPCNT', KEEPALIVE_PROBES)):
            option = getattr(socket, name, None)
            if option is not None:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
        return True
    except OSError as error:
        log.warning('could not enable TCP keepalive for %s: %s', label, error)
        return False


def _read_available(port, limit: int = 4096) -> bytes:
    """Read whatever is there. A driver buffer makes bytes readable that select does not report.

    An I/O error is deliberately NOT swallowed: a port that has gone away must end the run, not look
    like an idle device until the duration bound expires.
    """
    waiting = getattr(port, 'in_waiting', 0)
    return port.read(waiting or limit)


def _wait_for_banner(port, marker: bytes, timeout: float, clock: Callable[[], float]) -> bytes:
    """Consume the device's startup banner. It is host bookkeeping, not protocol traffic."""
    seen = bytearray()
    deadline = clock() + timeout
    while clock() < deadline:
        readable, _, _ = select.select([port], [], [], 0.2)
        if not readable and not getattr(port, 'in_waiting', 0):
            continue
        chunk = _read_available(port)
        if not chunk:
            continue
        seen.extend(chunk)
        if marker in seen:
            break
    return bytes(seen)


def run_relay(
    serial_factory: Callable[[], Any],
    connect: Callable[[Tuple[str, int]], socket.socket],
    *,
    endpoint: Tuple[str, int],
    duration: float,
    mode_command: bytes = DEFAULT_MODE_COMMAND,
    expected_ack: bytes = DEFAULT_ACK,
    ready_marker: Optional[str] = None,
    ready_timeout: float = 20.0,
    ack_timeout: float = 8.0,
    expect_device_id: Optional[str] = None,
    stop_when_registered: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> RelayOutcome:
    """Put the device into link mode and pipe it to the gateway until either end stops.

    With `stop_when_registered` the run ends the moment the gateway answers the registration, which is
    what an acceptance check wants: the shortest possible window on a live listener.
    """
    outcome = RelayOutcome()
    port = serial_factory()
    gateway_socket: Optional[socket.socket] = None
    upstream = FrameObserver()
    downstream = FrameObserver()
    try:
        if ready_marker is not None:
            banner = _wait_for_banner(port, ready_marker.encode('ascii'), ready_timeout, clock)
            if ready_marker.encode('ascii') not in banner:
                outcome.stopped_because = 'no ready marker'
                outcome.error = f'the device never printed {ready_marker!r} within {ready_timeout:.0f}s'
                return outcome
        else:
            reset = getattr(port, 'reset_input_buffer', None)
            if reset is not None:
                reset()

        if port.write(mode_command) != len(mode_command):
            outcome.stopped_because = 'mode command not written'
            outcome.error = 'the mode command was not written in full'
            return outcome

        seen = bytearray()
        acknowledged = False
        ack_deadline = clock() + ack_timeout
        while clock() < ack_deadline:
            readable, _, _ = select.select([port], [], [], 0.2)
            if not readable and not getattr(port, 'in_waiting', 0):
                continue
            chunk = _read_available(port)
            if not chunk:
                continue
            seen.extend(chunk)
            if expected_ack in seen:
                acknowledged = True
                break
        if not acknowledged:
            outcome.stopped_because = 'no mode acknowledgment'
            outcome.error = (f'the device did not acknowledge the mode command within {ack_timeout:.0f}s: '
                             f'{bytes(seen)[-160:]!r}')
            return outcome
        log.info('device acknowledged the mode command')

        try:
            gateway_socket = connect(endpoint)
        except OSError as error:
            outcome.stopped_because = f'gateway unreachable ({error})'
            return outcome
        enable_tcp_keepalive(gateway_socket)
        log.info('relaying to %s:%s', *endpoint)

        deadline = clock() + duration
        while clock() < deadline:
            readable, _, _ = select.select([port, gateway_socket], [], [], 0.2)
            if port in readable or getattr(port, 'in_waiting', 0):
                chunk = _read_available(port)
                if chunk:
                    outcome.bytes_from_device += len(chunk)
                    upstream.feed(chunk)
                    gateway_socket.sendall(chunk)
            if gateway_socket in readable:
                chunk = gateway_socket.recv(4096)
                if not chunk:
                    outcome.stopped_because = 'gateway closed the connection'
                    break
                outcome.bytes_to_device += len(chunk)
                downstream.feed(chunk)
                port.write(chunk)
                if stop_when_registered and (downstream.counts.get(REGISTER_OK_EVENT)
                                             or downstream.counts.get(REGISTER_REFUSED_EVENT)):
                    outcome.stopped_because = ('registered' if downstream.counts.get(REGISTER_OK_EVENT)
                                               else 'refused')
                    break
        else:
            outcome.stopped_because = 'duration reached'
    except (OSError, ValueError) as error:
        outcome.stopped_because = outcome.stopped_because or f'transport error ({error})'
        outcome.error = outcome.error or str(error)
    finally:
        if gateway_socket is not None:
            try:
                gateway_socket.close()
            except OSError:
                pass
        close = getattr(port, 'close', None)
        if close is not None:
            close()

    outcome.frames_from_device = upstream.counts
    outcome.frames_to_device = downstream.counts
    registered = upstream.data.get(REGISTER_EVENT) or {}
    outcome.device_id = registered.get('device_id')
    refusal = downstream.data.get(REGISTER_REFUSED_EVENT) or {}
    if downstream.counts.get(REGISTER_REFUSED_EVENT):
        outcome.refused = True
        outcome.refused_reason = str(refusal.get('reason', ''))
    outcome.registered = downstream.counts.get(REGISTER_OK_EVENT, 0) == 1
    if outcome.registered and expect_device_id is not None and outcome.device_id != expect_device_id:
        outcome.mismatch = True
    return outcome


def open_serial(port_path: str, baudrate: int = 115200):
    """Open the device port without asserting DTR or RTS, which would reset some boards."""
    import serial  # imported here so the module stays importable without pyserial

    connection = serial.Serial(port=None, baudrate=baudrate, timeout=0.2, write_timeout=5)
    connection.dtr = False
    connection.rts = False
    connection.port = port_path
    connection.open()
    return connection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Relay a USB-attached satellite to a gateway registry listener.')
    parser.add_argument('--port', required=True,
                        help='serial port of the device, e.g. /dev/serial/by-id/usb-...')
    parser.add_argument('--gateway', required=True, help='HOST or HOST:PORT of the gateway listener')
    parser.add_argument('--duration', type=float, default=30.0, help='seconds to relay before stopping')
    parser.add_argument('--expect-device-id', default=None,
                        help='fail with exit 5 unless the device registers under this id')
    parser.add_argument('--ready-marker', default=None,
                        help='optional banner to wait for before sending the mode command')
    parser.add_argument('--ready-timeout', type=float, default=20.0)
    parser.add_argument('--ack-timeout', type=float, default=8.0)
    parser.add_argument('--connect-timeout', type=float, default=10.0)
    parser.add_argument('--stop-after-registration', action='store_true',
                        help='end the run as soon as the gateway answers the registration')
    parser.add_argument('--verbose', action='store_true')
    return parser


def main(argv: Optional[list] = None) -> int:
    arguments = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if arguments.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        endpoint = parse_endpoint(arguments.gateway)
    except ValueError as error:
        print(f'bad gateway endpoint: {error}', file=sys.stderr)
        return EXIT_DEVICE_LINK_FAILED
    outcome = run_relay(
        lambda: open_serial(arguments.port),
        lambda target: socket.create_connection(target, timeout=arguments.connect_timeout),
        endpoint=endpoint,
        duration=arguments.duration,
        ready_marker=arguments.ready_marker,
        ready_timeout=arguments.ready_timeout,
        ack_timeout=arguments.ack_timeout,
        expect_device_id=arguments.expect_device_id,
        stop_when_registered=arguments.stop_after_registration,
    )
    print(json.dumps({
        'registered': outcome.registered,
        'refused': outcome.refused,
        'device_id': outcome.device_id,
        'refused_reason': outcome.refused_reason,
        'gateway': f'{endpoint[0]}:{endpoint[1]}',
        'frames_from_device': outcome.frames_from_device,
        'frames_to_device': outcome.frames_to_device,
        'bytes_from_device': outcome.bytes_from_device,
        'bytes_to_device': outcome.bytes_to_device,
        'stopped_because': outcome.stopped_because,
        'error': outcome.error,
        'exit_code': outcome.exit_code,
    }, indent=2))
    return outcome.exit_code


if __name__ == '__main__':
    raise SystemExit(main())
