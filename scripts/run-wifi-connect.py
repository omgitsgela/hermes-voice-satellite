#!/usr/bin/env python3
"""Stage credentials on the device, ask it to join, and print the report.

The passphrase is read from a file, so it never appears in a process listing or shell history, and
it is never printed here at all: the console does not echo what it receives, and the join report
carries no key material.

The serial port comes from --port or HERMES_SATELLITE_SERIAL: nothing in this tree is a device path.

Usage: run-wifi-connect.py <psk-file> <ssid> [--port <usb>]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from serial_console import open_console, resolve_port, wait_for  # noqa: E402

READY = b'PROVISION READY'
JOIN_TIMEOUT = 45  # the device gives up on its own after 20s; this is the outer bound


def read_psk(path) -> bytes:
    """The passphrase file, with only the line terminator stripped. A WPA passphrase may
    legally contain leading or trailing spaces, and stripping them would silently stage a
    wrong key that the join then fails with a misleading reason -- the same contract
    esp32_provision.read_psk pins for the provisioning tool."""
    with open(path, 'rb') as handle:
        psk = handle.read().rstrip(b'\r\n')
    if not psk:
        raise SystemExit('the passphrase file is empty')
    return psk


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('psk_file', help='file holding the passphrase; read, never printed')
    parser.add_argument('ssid')
    parser.add_argument('--port', help='ESP32 USB serial device; or set HERMES_SATELLITE_SERIAL')
    args = parser.parse_args()
    port = resolve_port(args.port)

    psk = read_psk(args.psk_file)

    connection = open_console(port)
    try:
        connection.reset_input_buffer()
        _, ready = wait_for(connection, READY, 25, echo=sys.stdout)
        if not ready:
            print('\nNO READY MARKER: the console did not come up')
            raise SystemExit(1)

        connection.write(f'SET_WIFI_SSID {args.ssid}\n'.encode())
        connection.flush()
        _, staged_ssid = wait_for(connection, b'PROVISION ', 10)

        connection.write(b'SET_WIFI_PSK ' + psk + b'\n')
        connection.flush()
        _, staged_psk = wait_for(connection, b'PROVISION ', 10)

        print(f'\n--- staged: ssid={staged_ssid} psk={staged_psk}; joining ---')
        connection.write(b'WIFI_CONNECT\n')
        connection.flush()
        block, finished = wait_for(connection, b'PROVISION WIFI CONNECT END', JOIN_TIMEOUT,
                                   echo=sys.stdout)
    finally:
        connection.close()

    if not finished:
        print('\nINCOMPLETE: no join block within the deadline')
        raise SystemExit(3)
    if b'status=connected' in block:
        print('\nJOIN SUCCEEDED')
        raise SystemExit(0)
    print('\nJOIN FAILED (see the reason code in the report)')
    raise SystemExit(2)


if __name__ == '__main__':
    main()
