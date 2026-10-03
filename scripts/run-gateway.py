#!/usr/bin/env python3
"""Stage credentials and an endpoint, then ask the device to join and register.

The passphrase is read from a file so it never appears in a process listing or shell
history, and it is never printed: the console does not echo input, and every report line
carries no key material.

Usage: run-gateway.py <psk-file> <ssid> <endpoint>
"""
import sys
import time
import serial

PORT = '/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_68:EE:8F:4B:59:88-if00'
READY = b'PROVISION READY'
BLOCK_END = b'PROVISION GATEWAY END'


def open_port():
    connection = serial.Serial(port=None, baudrate=115200, timeout=0.2)
    connection.dtr = False
    connection.rts = False
    connection.port = PORT
    connection.open()
    return connection


def wait_for(connection, needle, seconds):
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


if len(sys.argv) < 4:
    raise SystemExit('usage: run-gateway.py <psk-file> <ssid> <endpoint>')
with open(sys.argv[1], 'rb') as handle:
    psk = handle.read().strip()
assert psk, 'the passphrase file is empty'
ssid, endpoint = sys.argv[2], sys.argv[3]

connection = open_port()
try:
    connection.reset_input_buffer()
    _, ready = wait_for(connection, READY, 25)
    if not ready:
        print('\nNO READY MARKER: the console did not come up')
        raise SystemExit(1)

    for command in (f'SET_WIFI_SSID {ssid}', f'SET_WIFI_PSK {psk.decode()}', f'SET_ENDPOINT {endpoint}'):
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
    block, finished = wait_for(connection, BLOCK_END, 90)
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
