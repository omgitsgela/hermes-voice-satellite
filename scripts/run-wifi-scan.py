#!/usr/bin/env python3
"""Ask the device to run a console command that frames its report, and show the framed reply.

Defaults to WIFI_SCAN. Opening this port resets the chip (native USB-Serial/JTAG), so the script
waits for the console's ready marker before sending anything, and treats the marker as the only
signal that the console is accepting commands.

The serial port comes from --port or HERMES_SATELLITE_SERIAL: nothing in this tree is a device path.

Usage: run-wifi-scan.py [--command WIFI_SCAN] [--port <usb>]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from serial_console import open_console, resolve_port, wait_for  # noqa: E402

READY = b'PROVISION READY'
END = b'SCAN END'


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--command', default='WIFI_SCAN',
                        help='a command whose reply ends with SCAN END (default WIFI_SCAN)')
    parser.add_argument('--port', help='ESP32 USB serial device; or set HERMES_SATELLITE_SERIAL')
    args = parser.parse_args()
    port = resolve_port(args.port)

    connection = open_console(port)
    try:
        connection.reset_input_buffer()
        _, ready = wait_for(connection, READY, 25, echo=sys.stdout)
        if not ready:
            print('\nNO READY MARKER: the console did not come up', flush=True)
            raise SystemExit(1)
        print(f'\n--- console ready; sending {args.command} ---', flush=True)
        connection.write(args.command.encode() + b'\n')
        connection.flush()
        _, finished = wait_for(connection, END, 60, echo=sys.stdout)
    finally:
        connection.close()

    if not finished:
        print(f'\nINCOMPLETE: no {END.decode()} marker within the deadline', flush=True)
        raise SystemExit(1)
    print('\nCOMMAND COMPLETED', flush=True)


if __name__ == '__main__':
    main()
