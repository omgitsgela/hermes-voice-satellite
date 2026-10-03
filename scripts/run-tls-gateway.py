#!/usr/bin/env python3
"""Ask the device to register over TLS, and check the report against what the host expects.

The Wi-Fi passphrase is read from a file so it never appears in a process listing or shell history,
and it is never printed. The expected server fingerprint is passed in rather than read back from the
device: a check that takes its answer from the thing it is checking is not a check.

The serial port comes from --port or HERMES_SATELLITE_SERIAL: nothing in this tree is a device path.

Usage: run-tls-gateway.py [--expect-failure <substring>] <psk-file> <ssid> <endpoint> <pin-hex>
       run-tls-gateway.py [--expect-failure <substring>] --no-wifi <endpoint> <pin-hex>

With --expect-failure, the run must NOT register and the report must contain the substring: that is
how the negative cases are checked, rather than assuming a failure means the right thing failed.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from serial_console import open_console, resolve_port, wait_for  # noqa: E402

READY = b'PROVISION READY'
BLOCK_END = b'PROVISION GATEWAY END'


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--expect-failure', metavar='SUBSTRING',
                        help='the run must not register and must show this substring in the report')
    parser.add_argument('--no-wifi', action='store_true',
                        help='register from an already-joined network; take no passphrase')
    parser.add_argument('psk_file', nargs='?', help='file holding the passphrase; read, never printed')
    parser.add_argument('ssid', nargs='?')
    parser.add_argument('endpoint', help='tcp://host:port or tls://host:port')
    parser.add_argument('pin_hex', help='the server fingerprint the host computed, not the device')
    parser.add_argument('--port', help='ESP32 USB serial device; or set HERMES_SATELLITE_SERIAL')
    args = parser.parse_args()
    port = resolve_port(args.port)

    psk = ssid = None
    if not args.no_wifi:
        if not (args.psk_file and args.ssid):
            raise SystemExit('usage: <psk-file> <ssid> <endpoint> <pin> | --no-wifi <endpoint> <pin>')
        with open(args.psk_file, 'rb') as handle:
            psk = handle.read().strip()
        assert psk, 'the passphrase file is empty'
        ssid = args.ssid
    endpoint, expected_pin = args.endpoint, args.pin_hex

    connection = open_console(port)
    try:
        connection.reset_input_buffer()
        _, ready = wait_for(connection, READY, 25, echo=sys.stdout)
        if not ready:
            print('\nNO READY MARKER: the console did not come up')
            raise SystemExit(1)

        # What the device believes it has provisioned, before anything is dialled.
        connection.write(b'TLS_STATUS\n')
        connection.flush()
        status, replied = wait_for(connection, b'PROVISION OK TLS', 10)
        if not replied:
            print('\nTLS_STATUS DID NOT REPLY')
            raise SystemExit(1)
        text = status.decode('utf-8', errors='replace')
        if f'pin={expected_pin}' not in text:
            print(f'\nTHE DEVICE REPORTS A DIFFERENT PIN THAN THE HOST COMPUTED ({expected_pin})')
            raise SystemExit(4)

        commands = []
        if psk is not None:
            commands += [f'SET_WIFI_SSID {ssid}', f'SET_WIFI_PSK {psk.decode()}']
        commands.append(f'SET_ENDPOINT {endpoint}')
        for command in commands:
            connection.write(command.encode() + b'\n')
            connection.flush()
            _, acknowledged = wait_for(connection, b'PROVISION ', 10)
            if not acknowledged:
                print(f'\nNO ACKNOWLEDGEMENT for {command.split(" ")[0]}')
                raise SystemExit(1)

        print('\n--- staging confirmed; asking the device to join and register over TLS ---')
        connection.write(b'START_GATEWAY\n')
        connection.flush()
        block, finished = wait_for(connection, BLOCK_END, 120, echo=sys.stdout)
    finally:
        connection.close()

    report = block.decode('utf-8', errors='replace')
    if not finished:
        print('\nINCOMPLETE: no gateway block within the deadline')
        raise SystemExit(3)

    print()
    for line in report.splitlines():
        if line.startswith(('PROVISION GATEWAY clock=', 'PROVISION GATEWAY tls=',
                            'PROVISION GATEWAY session=', 'PROVISION GATEWAY socket=',
                            'PROVISION GATEWAY resource=')):
            print('  ' + line)

    if args.expect_failure:
        if 'session=registered' in report:
            print(f'IT REGISTERED, WHICH IS THE OPPOSITE OF THE EXPECTED FAILURE: {args.expect_failure}')
            raise SystemExit(2)
        if args.expect_failure not in report:
            print(f'THE REPORT DOES NOT SHOW THE EXPECTED FAILURE: {args.expect_failure}')
            raise SystemExit(2)
        print(f'REFUSED AS EXPECTED: {args.expect_failure}')
        raise SystemExit(0)

    required = ('clock=', 'tls=handshake ok', 'tls=server_pin matched', 'session=registered')
    missing = [needle for needle in required if needle not in report]
    if missing:
        print('THE REPORT IS MISSING: ' + ', '.join(missing))
        raise SystemExit(2)
    print('REGISTERED OVER TLS')


if __name__ == '__main__':
    main()
