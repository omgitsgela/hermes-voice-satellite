#!/usr/bin/env python3
"""Stage credentials on the device, ask it to join, and print the report.

The passphrase is read from a file, so it never appears in a process listing or shell
history, and it is never printed here at all: the console does not echo what it receives,
and the join report carries no key material.

Usage: run-wifi-connect.py <psk-file> <ssid>
"""
import sys
import time
import serial

PORT = '/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_68:EE:8F:4B:59:88-if00'
SSID = sys.argv[2] if len(sys.argv) > 2 else ''
READY = b'PROVISION READY'
JOIN_TIMEOUT = 45  # the device gives up on its own after 20s; this is the outer bound


def open_port():
    # DTR/RTS low, or the chip is held in reset and never boots.
    connection = serial.Serial(port=None, baudrate=115200, timeout=0.2)
    connection.dtr = False
    connection.rts = False
    connection.port = PORT
    connection.open()
    return connection


def wait_for(connection, needle, seconds):
    """Read and echo until `needle` appears, the deadline passes, or the port goes away."""
    received = bytearray()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            chunk = connection.read(512)
        except (serial.SerialException, OSError):
            return received, False
        if chunk:
            sys.stdout.write(chunk.decode('utf-8', errors='replace'))
            sys.stdout.flush()
            received.extend(chunk)
            if needle in received:
                return received, True
    return received, False


with open(sys.argv[1], 'rb') as handle:
    psk = handle.read().strip()
assert psk, 'the passphrase file is empty'
assert SSID, 'usage: run-wifi-connect.py <psk-file> <ssid>'

connection = open_port()
try:
    connection.reset_input_buffer()
    boot, ready = wait_for(connection, READY, 25)
    if not ready:
        print('\nNO READY MARKER: the console did not come up')
        raise SystemExit(1)

    connection.write(f'SET_WIFI_SSID {SSID}\n'.encode())
    connection.flush()
    _, staged_ssid = wait_for(connection, b'PROVISION ', 10)

    connection.write(b'SET_WIFI_PSK ' + psk + b'\n')
    connection.flush()
    _, staged_psk = wait_for(connection, b'PROVISION ', 10)

    print(f'\n--- staged: ssid={staged_ssid} psk={staged_psk}; joining ---')
    connection.write(b'WIFI_CONNECT\n')
    connection.flush()
    block, finished = wait_for(connection, b'PROVISION WIFI CONNECT END', JOIN_TIMEOUT)
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
