#!/usr/bin/env python3
"""Stage a non-secret gateway endpoint over USB. Never activates audio/network."""
import argparse
import time


def exchange(connection, command, timeout=5.0):
    """Connection reads/writes must themselves have finite timeouts."""
    if not command or len(command) > 255 or any(not 32 <= ord(c) <= 126 for c in command):
        raise ValueError('command must be at most 255 printable ASCII characters')
    if connection.write((command + '\n').encode('ascii')) != len(command) + 1:
        raise RuntimeError('partial serial write; close and resynchronize before retry')
    deadline = time.monotonic() + timeout
    line = bytearray()
    while time.monotonic() < deadline:
        chunk = connection.read(1)
        if not chunk:
            continue
        if chunk == b'\n':
            text = line.decode('ascii', errors='replace').rstrip('\r')
            line.clear()
            if text.startswith('PROVISION ERR '):
                raise RuntimeError(text.removeprefix('PROVISION '))
            if text.startswith('PROVISION OK '):
                return text.removeprefix('PROVISION ')
        else:
            line.extend(chunk)
            if len(line) > 1024:
                raise RuntimeError('oversize serial response')
    raise TimeoutError('no provisioning acknowledgment within deadline')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, help='ESP32 USB serial device, not XMOS audio USB')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('status')
    endpoint = sub.add_parser('set-endpoint')
    endpoint.add_argument('endpoint', help='tcp://host:port (trusted LAN) or tls://host:port; staging only')
    sub.add_parser('clear-endpoint')
    args = parser.parse_args()
    command = {'status': 'STATUS', 'clear-endpoint': 'CLEAR_ENDPOINT'}.get(args.action)
    if args.action == 'set-endpoint':
        command = 'SET_ENDPOINT ' + args.endpoint
    import serial
    # Set modem control before opening: ordinary commands must not reset the MCU.
    connection = serial.Serial(port=None, baudrate=115200, timeout=0.2, write_timeout=2)
    connection.dtr = False
    connection.rts = False
    connection.port = args.port
    try:
        connection.open()
        connection.reset_input_buffer()
        print(exchange(connection, command))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
