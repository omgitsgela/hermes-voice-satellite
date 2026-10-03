#!/usr/bin/env python3
"""Stage credentials and an endpoint, then ask the device to join and register.

The passphrase is read from a file so it never appears in a process listing or shell history, and
it is never printed: the console does not echo input, and every report line carries no key material.

The serial port comes from --port or HERMES_SATELLITE_SERIAL: nothing in this tree is a device path.

Usage: run-gateway.py <psk-file> <ssid> <endpoint> [--port <usb>]
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
    parser.add_argument('psk_file', help='file holding the passphrase; read, never printed')
    parser.add_argument('ssid')
    parser.add_argument('endpoint', help='tcp://host:port or tls://host:port')
    parser.add_argument('--port', help='ESP32 USB serial device; or set HERMES_SATELLITE_SERIAL')
    args = parser.parse_args()
    port = resolve_port(args.port)

    with open(args.psk_file, 'rb') as handle:
        psk = handle.read().strip()
    assert psk, 'the passphrase file is empty'

    connection = open_console(port)
    try:
        connection.reset_input_buffer()
        _, ready = wait_for(connection, READY, 25, echo=sys.stdout)
        if not ready:
            print('\nNO READY MARKER: the console did not come up')
            raise SystemExit(1)

        for command in (f'SET_WIFI_SSID {args.ssid}', f'SET_WIFI_PSK {psk.decode()}',
                        f'SET_ENDPOINT {args.endpoint}'):
            connection.write(command.encode() + b'\n')
            connection.flush()
            _, acknowledged = wait_for(connection, b'PROVISION ', 10)
            if not acknowledged:
                print(f'\nNO ACKNOWLEDGEMENT for {command.split(" ")[0]}')
                raise SystemExit(1)

        # Readiness re-checked from the device's own view before asking it to do anything.
        connection.write(b'WIFI_STATUS\n')
        connection.flush()
        wait_for(connection, b'PROVISION ', 10)

        print('\n--- staging confirmed; asking the device to join and register ---')
        connection.write(b'START_GATEWAY\n')
        connection.flush()
        block, finished = wait_for(connection, BLOCK_END, 90, echo=sys.stdout)
    finally:
        connection.close()

    if not finished:
        print('\nINCOMPLETE: no gateway block within the deadline')
        raise SystemExit(3)
    if b'session=registered' in block:
        print('\nREGISTERED OVER WI-FI')
        raise SystemExit(0)
    print('\nGATEWAY RUN DID NOT REACH REGISTRATION')
    raise SystemExit(2)


if __name__ == '__main__':
    main()
