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

#: The exact lines the device acks each staging step with (provisioning.c). They must be matched
#: whole, never as the b'PROVISION ' prefix: a rejected stage answers `PROVISION ERR ...`, which
#: starts with the same prefix, and consuming that refusal as a successful acknowledgment would
#: stage nothing, send the join anyway, and misreport a credential error as "no join block".
SSID_ACK = b'PROVISION OK WIFI SSID STAGED'
PSK_ACK = b'PROVISION OK WIFI PSK STAGED'


def psk_shape(psk: bytes) -> str:
    """The same shape rule the provisioning tool enforces before a byte reaches the device: an
    8-63 character passphrase, or a 64-hex raw key. A malformed file used to sail right past
    this script and get misdiagnosed as an incomplete join block."""
    if len(psk) == 64 and all(byte in b'0123456789abcdefABCDEF' for byte in psk):
        return '64-hex-raw-key'
    if 8 <= len(psk) <= 63 and all(32 <= byte <= 126 for byte in psk):
        return 'ascii-passphrase'
    return ''


def read_psk(path) -> bytes:
    """The passphrase file, with only the line terminator stripped, validated before a byte
    reaches the device. A WPA passphrase may legally contain leading or trailing spaces, and
    stripping them would silently stage a wrong key that the join then fails with a misleading
    reason -- the same contract esp32_provision.read_psk pins for the provisioning tool. The
    shape rule is the tool's own, so a malformed file fails here, not as a phantom join error."""
    with open(path, 'rb') as handle:
        psk = handle.read().rstrip(b'\r\n')
    if not psk:
        raise SystemExit('the passphrase file is empty')
    if not psk_shape(psk):
        raise SystemExit(f'the passphrase is neither an 8-63 character passphrase nor a '
                         f'64-hex raw key ({len(psk)} bytes)')
    return psk


def run(connection, ssid: str, psk: bytes, stage_timeout: float = 10.0,
        ready_timeout: float = 25.0, join_timeout: int = JOIN_TIMEOUT,
        echo=None) -> int:
    """Stage the pair, ask the device to join, and return the exit code.

    `connection` is anything with write/flush/read/close/reset_input_buffer -- the real serial
    port on the device, or a scripted stand-in in a host test. A stage the device refuses (or a
    console that goes quiet) is a complete answer: the join is not sent, and the refusal is
    surfaced as exit 2 rather than misreported as an incomplete join block.
    """
    connection.reset_input_buffer()
    _, ready = wait_for(connection, READY, ready_timeout, echo=echo)
    if not ready:
        if echo is not None:
            echo.write('\nNO READY MARKER: the console did not come up')
            echo.flush()
        return 1

    connection.write(f'SET_WIFI_SSID {ssid}\n'.encode())
    connection.flush()
    _, staged_ssid = wait_for(connection, SSID_ACK, stage_timeout, echo=echo)

    connection.write(b'SET_WIFI_PSK ' + psk + b'\n')
    connection.flush()
    _, staged_psk = wait_for(connection, PSK_ACK, stage_timeout, echo=echo)

    # A stage the device refused is a complete answer: the join would fail anyway, and sending
    # it would burn the join budget on an error that was already in hand.
    if not staged_ssid or not staged_psk:
        if echo is not None:
            echo.write('\nSTAGE REFUSED: the device did not acknowledge both fields; '
                       'see the PROVISION line above (SSID and/or PSK refused or the console '
                       'went quiet)')
            echo.flush()
        return 2

    if echo is not None:
        echo.write('\n--- staged: both fields acknowledged; joining ---')
        echo.flush()
    connection.write(b'WIFI_CONNECT\n')
    connection.flush()
    block, finished = wait_for(connection, b'PROVISION WIFI CONNECT END', join_timeout, echo=echo)

    if not finished:
        if echo is not None:
            echo.write('\nINCOMPLETE: no join block within the deadline')
            echo.flush()
        return 3
    if b'status=connected' in block:
        if echo is not None:
            echo.write('\nJOIN SUCCEEDED')
            echo.flush()
        return 0
    if echo is not None:
        echo.write('\nJOIN FAILED (see the reason code in the report)')
        echo.flush()
    return 2


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
        return run(connection, args.ssid, psk, echo=sys.stdout)
    finally:
        connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
