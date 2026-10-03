#!/usr/bin/env python3
"""Provision a satellite over USB: stage its gateway endpoint and the Wi-Fi networks it may join.

Everything the console can be given is here, and nothing that is not: the endpoint (what it dials),
the saved networks (what it joins, held across reboots and the thing register-on-boot needs), and
the one-shot actions that prove them (list, connect, scan). It never activates audio, and it never
prints a passphrase: a passphrase is read from a file, sent as raw bytes, and the device reports its
presence, not its value.

Public-safe on purpose: the port is always an argument (no device path is baked in), and a saved
network's passphrase is a file reference, so the secret never appears on a command line, in the
process list, or in the output.

Usage:
    esp32_provision.py --port <usb> status
    esp32_provision.py --port <usb> set-endpoint <uri>
    esp32_provision.py --port <usb> clear-endpoint
    esp32_provision.py --port <usb> save-profile <ssid> <psk-file>
    esp32_provision.py --port <usb> list-profiles
    esp32_provision.py --port <usb> remove-profile <ssid>
    esp32_provision.py --port <usb> clear-profiles
    esp32_provision.py --port <usb> connect
    esp32_provision.py --port <usb> scan
"""
import argparse
import time

#: A WPA2/WPA3 passphrase is 8-63 printable characters, or a 64-hex-digit raw PSK. Both are valid
#: to stage; anything else is rejected before a byte reaches the device.
def psk_shape(psk: bytes) -> str:
    if len(psk) == 64 and all(byte in b'0123456789abcdefABCDEF' for byte in psk):
        return '64-hex-raw-key'
    if 8 <= len(psk) <= 63 and all(32 <= byte <= 126 for byte in psk):
        return 'ascii-passphrase'
    return ''


def read_psk(path) -> bytes:
    """A passphrase from a file, stripped of its terminator, validated before it is ever sent.
    The file is the one place the secret lives: it is read, used, and not printed."""
    with open(path, 'rb') as handle:
        psk = handle.read().strip()
    if not psk:
        raise SystemExit(f'{path} is empty')
    if not psk_shape(psk):
        raise SystemExit(f'{path} is neither an 8-63 character passphrase nor a 64-hex raw key '
                         f'({len(psk)} bytes)')
    return psk


def exchange(connection, command, timeout=5.0):
    """One command, one `PROVISION ...` line. Connection reads/writes must themselves have finite
    timeouts."""
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


def exchange_block(connection, command, terminator=None, timeout=30.0):
    """One command whose reply is a block of lines, not a single acknowledgment.

    The reply is collected line by line until it is over: either `terminator` is received (the
    framed commands -- WIFI_SCAN, WIFI_CONNECT, START_GATEWAY -- all end with an explicit ... END
    marker), or the console goes quiet for two reads in a row after it has already sent a line
    (the profile listing has no end marker; the device sends it all in one burst and then stops).
    Returns the lines, joined, with the leading `PROVISION ` prefix left on each as the device
    sent it, so a caller can tell exactly what was reported."""
    if not command or len(command) > 255:
        raise ValueError('command must be at most 255 characters')
    if connection.write((command + '\n').encode('ascii')) != len(command) + 1:
        raise RuntimeError('partial serial write; close and resynchronize before retry')
    deadline = time.monotonic() + timeout
    lines = []
    current = bytearray()
    empty_reads = 0
    while time.monotonic() < deadline:
        chunk = connection.read(1)
        if not chunk:
            empty_reads += 1
            # Two consecutive quiet reads mean the burst is done. Only meaningful once we have
            # something: a command that replies with a single line is handled by exchange().
            if empty_reads >= 2 and lines:
                break
            continue
        empty_reads = 0
        if chunk == b'\n':
            text = current.decode('ascii', errors='replace').rstrip('\r')
            current.clear()
            if text:
                lines.append(text)
                if terminator and terminator in text:
                    break
            continue
        current.extend(chunk)
        if len(current) > 4096:
            raise RuntimeError('oversize serial response line')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', required=True, help='ESP32 USB serial device, not XMOS audio USB')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('status')
    endpoint = sub.add_parser('set-endpoint')
    endpoint.add_argument('endpoint',
                          help='tcp://host:port (trusted LAN) or tls://host:port; staging only')
    sub.add_parser('clear-endpoint')
    save = sub.add_parser('save-profile')
    save.add_argument('ssid')
    save.add_argument('psk_file', help='file holding the passphrase; read, never printed')
    sub.add_parser('list-profiles')
    remove = sub.add_parser('remove-profile')
    remove.add_argument('ssid')
    sub.add_parser('clear-profiles')
    sub.add_parser('connect')
    sub.add_parser('scan')
    args = parser.parse_args()

    command = {'status': 'STATUS', 'clear-endpoint': 'CLEAR_ENDPOINT',
               'list-profiles': 'WIFI_PROFILES', 'clear-profiles': 'WIFI_PROFILE_CLEAR',
               'connect': 'WIFI_CONNECT', 'scan': 'WIFI_SCAN'}.get(args.action)
    if args.action == 'set-endpoint':
        command = 'SET_ENDPOINT ' + args.endpoint
    elif args.action == 'save-profile':
        command = f'SET_WIFI_SSID {args.ssid}'
    elif args.action == 'remove-profile':
        command = f'WIFI_PROFILE_REMOVE {args.ssid}'

    import serial
    # Set modem control before opening: ordinary commands must not reset the MCU.
    connection = serial.Serial(port=None, baudrate=115200, timeout=0.2, write_timeout=2)
    connection.dtr = False
    connection.rts = False
    connection.port = args.port
    try:
        connection.open()
        connection.reset_input_buffer()
        if args.action == 'save-profile':
            # Stage the pair, then save it as a profile. The PSK is sent straight from the file.
            print(exchange(connection, command))
            psk = read_psk(args.psk_file)
            if connection.write(b'SET_WIFI_PSK ' + psk + b'\n') != 12 + len(psk):
                raise RuntimeError('partial serial write of the passphrase')
            print(exchange(connection, 'WIFI_PROFILE_SAVE'))
        elif args.action in ('list-profiles', 'connect', 'scan'):
            # The listing has no end marker (it stops when the console goes quiet); the framed
            # actions all end with an explicit ... END line.
            print(exchange_block(connection, command, terminator='END'
                                 if args.action != 'list-profiles' else None,
                                 timeout=45.0))
        else:
            print(exchange(connection, command))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
