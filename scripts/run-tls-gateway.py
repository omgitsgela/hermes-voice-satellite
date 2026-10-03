#!/usr/bin/env python3
"""Ask the device to register over TLS, and check the report against what the host expects.

The Wi-Fi passphrase is read from a file so it never appears in a process listing or shell history,
and it is never printed. The expected server fingerprint is passed in rather than read back from the
device: a check that takes its answer from the thing it is checking is not a check.

Usage: run-tls-gateway.py [--expect-failure <substring>] <psk-file> <ssid> <endpoint> <pin-hex>
       run-tls-gateway.py [--expect-failure <substring>] --no-wifi <endpoint> <pin-hex>

With --expect-failure, the run must NOT register and the report must contain the substring: that is
how the negative cases are checked, rather than assuming a failure means the right thing failed.
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


arguments = sys.argv[1:]
expect_failure = None
if arguments and arguments[0] == '--expect-failure':
    expect_failure = arguments[1]
    arguments = arguments[2:]
if arguments and arguments[0] == '--no-wifi':
    psk = None
    ssid = None
    endpoint, expected_pin = arguments[1], arguments[2]
else:
    if len(arguments) < 4:
        raise SystemExit('usage: run-tls-gateway.py <psk-file> <ssid> <endpoint> <pin> | '
                         '--no-wifi <endpoint> <pin>')
    with open(arguments[0], 'rb') as handle:
        psk = handle.read().strip()
    assert psk, 'the passphrase file is empty'
    ssid, endpoint, expected_pin = arguments[1], arguments[2], arguments[3]

connection = open_port()
try:
    connection.reset_input_buffer()
    _, ready = wait_for(connection, READY, 25)
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
    block, finished = wait_for(connection, BLOCK_END, 120)
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

if expect_failure:
    if 'session=registered' in report:
        print(f'IT REGISTERED, WHICH IS THE OPPOSITE OF THE EXPECTED FAILURE: {expect_failure}')
        raise SystemExit(2)
    if expect_failure not in report:
        print(f'THE REPORT DOES NOT SHOW THE EXPECTED FAILURE: {expect_failure}')
        raise SystemExit(2)
    print(f'REFUSED AS EXPECTED: {expect_failure}')
    raise SystemExit(0)

required = ('clock=', 'tls=handshake ok', 'tls=server_pin matched', 'session=registered')
missing = [needle for needle in required if needle not in report]
if missing:
    print('THE REPORT IS MISSING: ' + ', '.join(missing))
    raise SystemExit(2)
print('REGISTERED OVER TLS')
