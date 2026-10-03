#!/usr/bin/env python3
"""Soak a running satellite: poll the read-only GATEWAY_STATUS command for a window and
report whether the registered link stayed alive the whole time.

The device must already be configured to register on boot (saved network plus a gateway
endpoint, so the persistent gateway task is running). This script never stages, joins,
dials, or starts anything; it reads one query per poll and judges the window. A sample
whose last line is a backoff is not a failure -- a dropped link that the supervisor
reconnects is the property under test. Failures are an IDLE sample (the task never ran or
terminated), a LOST sample (the console stopped answering), or a window in which the link
never registered.

Exit code 0 is a healthy window; 1 is anything else, so a soak is runnable by a person or
by a scheduled job alike.

Usage:
    run-soak.py --port <usb> --duration <seconds> [--interval <seconds>]
"""
import argparse
import time

from serial_console import open_console, resolve_port
import soak_harness
import soak_runner


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', help='ESP32 USB serial device (or set HERMES_SATELLITE_SERIAL)')
    parser.add_argument('--duration', type=float, required=True,
                        help='soak window, in seconds')
    parser.add_argument('--interval', type=float, default=1.0,
                        help='seconds between samples (default 1.0)')
    parser.add_argument('--sample-timeout', type=float, default=1.0,
                        help='seconds a single poll may take before the sample is LOST (default 1.0)')
    args = parser.parse_args()
    port = resolve_port(args.port)

    connection = open_console(port)
    try:
        connection.reset_input_buffer()
        verdict = soak_runner.run_soak(connection, args.duration, args.interval,
                                       time.monotonic, time.sleep,
                                       sample_timeout=args.sample_timeout)
    finally:
        connection.close()
    print(soak_harness.render_report(verdict))
    raise SystemExit(soak_runner.soak_exit(verdict))


if __name__ == '__main__':
    main()
