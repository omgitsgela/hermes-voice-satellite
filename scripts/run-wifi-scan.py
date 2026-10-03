#!/usr/bin/env python3
"""Ask the device to scan, and print the framed report.

Opening this port resets the chip (native USB-Serial/JTAG), so the script waits for the
console's ready marker before sending anything, and treats the marker as the only signal
that the console is accepting commands.
"""
import sys
import time
import serial

PORT = '/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_68:EE:8F:4B:59:88-if00'
READY = b'PROVISION READY'
END = b'SCAN END'
TARGET = sys.argv[1] if len(sys.argv) > 1 else 'WIFI_SCAN'


def open_port():
    # DTR/RTS must be driven low or the chip is held in reset and never boots.
    connection = serial.Serial(port=None, baudrate=115200, timeout=0.2)
    connection.dtr = False
    connection.rts = False
    connection.port = PORT
    connection.open()
    return connection


def pump(connection, deadline, stop_at=None):
    received = bytearray()
    while time.monotonic() < deadline:
        try:
            chunk = connection.read(512)
        except (serial.SerialException, OSError):
            return received, False
        if chunk:
            sys.stdout.write(chunk.decode('utf-8', errors='replace'))
            sys.stdout.flush()
            received.extend(chunk)
            if stop_at and stop_at in received:
                return received, True
    return received, False


connection = open_port()
connection.reset_input_buffer()
try:
    boot, ready = pump(connection, time.monotonic() + 25, stop_at=READY)
    if not ready:
        print('\nNO READY MARKER: the console did not come up', flush=True)
        raise SystemExit(1)
    print(f'\n--- console ready; sending {TARGET} ---', flush=True)
    connection.write(TARGET.encode() + b'\n')
    connection.flush()
    report, finished = pump(connection, time.monotonic() + 60, stop_at=END)
finally:
    connection.close()

if not finished:
    print(f'\nINCOMPLETE: no {END.decode()} marker within the deadline', flush=True)
    raise SystemExit(1)
print('\nSCAN COMMAND COMPLETED', flush=True)
