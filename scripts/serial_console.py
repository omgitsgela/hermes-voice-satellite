#!/usr/bin/env python3
"""Shared serial-console helpers for the device run scripts.

Two rules the run scripts used to each enforce for themselves, in one place:

* Public-safe ports. The serial port comes from an explicit argument, then the
  HERMES_SATELLITE_SERIAL environment variable, then a refusal. No device path is a
  constant in the tree: a shared checkout must not carry a specific satellite's
  USB identity, and a guess here is not recoverable.

* Bounded reads. wait_for reads until a needle arrives, the deadline passes, or the
  port dies. A dying port (pyserial's SerialException subclasses OSError, so one
  clause covers it) ends the stream as not-found rather than raising: the caller
  decides what an incomplete stream means, and a crash mid-report hides that
  decision.

pyserial is imported only when a real port is opened, so the pure parts run on a
host without the dependency and without a device attached.
"""
import os
import time

#: The one place a serial port may live when no argument is given. It is a variable,
#: not a default: setting it is a local configuration step, and an unset variable
#: is a visible refusal at the boundary.
PORT_ENV = 'HERMES_SATELLITE_SERIAL'


def resolve_port(port=None):
    """Argument, then environment, then a refusal that names both sources. Never a
    hardcoded device path: a shared repo must not carry a specific satellite's
    USB identity."""
    if port:
        return port
    from_env = os.environ.get(PORT_ENV)
    if from_env:
        return from_env
    raise SystemExit(f'no serial port: pass --port, or set {PORT_ENV}')


def open_console(port):
    """The ESP32 console: 115200, and DTR/RTS held low, because opening this chip's
    native USB-Serial/JTAG port with either line high holds it in reset and the
    console never boots. Imported here so the pure parts above stay pyserial-free."""
    import serial
    connection = serial.Serial(port=None, baudrate=115200, timeout=0.2)
    connection.dtr = False
    connection.rts = False
    connection.port = port
    connection.open()
    return connection


def wait_for(connection, needle, seconds, echo=None):
    """Read and (optionally) echo until `needle` appears, the deadline passes, or the
    port dies. Returns (what arrived, whether the needle was seen). The echo sink is
    anything with write/flush -- stdout by default of the callers -- and is optional
    so a host test can capture the stream instead."""
    received = bytearray()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            chunk = connection.read(512)
        except OSError:
            return received, False
        if chunk:
            if echo is not None:
                echo.write(chunk.decode('utf-8', errors='replace'))
                echo.flush()
            received.extend(chunk)
            if needle in received:
                return received, True
    return received, False
