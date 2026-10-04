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
    """A passphrase from a file, with only the line terminator stripped, validated before it is
    ever sent. The file is the one place the secret lives: it is read, used, and not printed.
    Only the trailing newline/carriage-return is removed: a WPA passphrase may legally contain
    leading or trailing spaces, and stripping them would silently stage a wrong key."""
    with open(path, 'rb') as handle:
        psk = handle.read()
    psk = psk.rstrip(b'\r\n')
    if not psk:
        raise SystemExit(f'{path} is empty')
    if not psk_shape(psk):
        raise SystemExit(f'{path} is neither an 8-63 character passphrase nor a 64-hex raw key '
                         f'({len(psk)} bytes)')
    return psk


def block_terminator(action: str):
    """The whole end-marker line that closes a framed block, or None for the unframed listing.

    Matched as a whole line, never a substring: a scan's AP lines are sent raw (no PROVISION
    prefix), so a network whose SSID contains the marker's text (WENDYS, BLEND, LEGEND, WEEKEND)
    would otherwise truncate the block and the real end marker would be left unread."""
    return {
        'scan': 'PROVISION WIFI SCAN END',
        'connect': 'PROVISION WIFI CONNECT END',
    }.get(action)


def save_profile(connection, ssid, psk_file, timeout=5.0):
    """Stage the Wi-Fi pair and save it as a profile, one acknowledged step at a time.

    Three commands go down the wire and each emits its own `PROVISION ...` line -- the SSID, the
    PSK, and the save. The save's result must be the SAVE's line: before this, the PSK was written
    raw with no acknowledgment read, so the `WIFI_PROFILE_SAVE` exchange consumed the PSK's stale
    ack and a save that the device refused (a full list, a bad commit) was reported as whatever
    the PSK had answered, while the real `ERR` line sat stranded in the buffer and the tool exited
    0. Routing every stage through exchange() both writes the exact byte count the write check
    expects (the PSK line is 13 + len(psk) + 1 bytes; the old 12 + len(psk) was two short, so the
    partial-write check raised on every passphrase) and consumes each acknowledgment before the
    next command, so a failed stage raises with the device's own reason. Returns the three lines
    so the caller reports exactly what the device acknowledged."""
    lines = [exchange(connection, f'SET_WIFI_SSID {ssid}', timeout=timeout)]
    # psk_shape guarantees every byte is printable ASCII, so the bytes decode to the same string
    # exchange will re-encode and validate; the raw form is never printed or logged.
    psk = read_psk(psk_file).decode('ascii')
    lines.append(exchange(connection, 'SET_WIFI_PSK ' + psk, timeout=timeout))
    lines.append(exchange(connection, 'WIFI_PROFILE_SAVE', timeout=timeout))
    return lines


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
                # Match the terminator as a whole line, not a substring: a scan's AP lines are
                # sent raw (no PROVISION prefix), so a network whose SSID merely contains the
                # marker's text (e.g. WENDYS) must not end the block early.
                if terminator is not None and text == terminator:
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
            # Stage the pair (each step acknowledged), then save it as a profile. The PSK is
            # read from its file and never printed; a refused save raises, so a failed save is
            # never reported as success.
            for line in save_profile(connection, args.ssid, args.psk_file):
                print(line)
        elif args.action in ('list-profiles', 'connect', 'scan'):
            # The listing has no end marker (it stops when the console goes quiet); the framed
            # actions all end with an explicit ... END line, matched whole.
            print(exchange_block(connection, command, terminator=block_terminator(args.action),
                                 timeout=45.0))
        else:
            print(exchange(connection, command))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
