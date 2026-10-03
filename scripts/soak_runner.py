#!/usr/bin/env python3
"""The thin glue between a serial connection and the soak verdict.

soak_harness owns the decision (and its unit tests own that decision); this module owns
the polling: send GATEWAY_STATUS, read the one PROVISION line that answers, and repeat
until the window is over. Three properties matter, and they are what the tests pin:

  * A silent or dead port is a SAMPLE, not a crash. A flapping USB link is exactly what a
    soak exists to find, so the sample is recorded as 'lost' and the soak continues to the
    next poll. A crash would end the very measurement it was started for.
  * The port, the clock, and the sleep are all arguments. The device's words (one
    PROVISION line per poll) come from the connection; when those words arrive is decided
    by the injected clock, not by the machine the soak happens to run on. That is what
    makes the loop unit-testable on a host with no board and no pyserial.
  * Only GATEWAY_STATUS goes on the wire. The soak is read-only by construction: it never
    stages, joins, dials, or starts anything.

pyserial is imported only when a real port is opened by the command-line entry point, so
the pure parts above run anywhere Python does.
"""
import soak_harness


def sample_status(connection, start_time, timeout, now):
    """One GATEWAY_STATUS poll. Returns (kind, last) from soak_harness.parse_status_line.

    Reads until the answer arrives, or the budget is spent, in which case the sample is
    ('lost', ''). The answer is a line that starts with 'OK GATEWAY ': everything else the
    console emits -- the boot banner, the boot-gate line, another command's reply, a log
    line -- is skipped, not mistaken for an answer. An 'OK GATEWAY ' line that matches
    neither known shape is protocol drift, and is reported as 'unexpected', not guessed at.
    The budget is the poll's own, enforced against the injected clock, so a slow or silent
    port cannot hold the soak."""
    deadline = start_time + timeout
    line = bytearray()
    if connection.write(b'GATEWAY_STATUS\n') != len(b'GATEWAY_STATUS\n'):
        raise RuntimeError('partial serial write; close and resynchronize before retry')
    while now() < deadline:
        try:
            chunk = connection.read(256)
        except OSError:
            return ('lost', '')
        if not chunk:
            continue
        line.extend(chunk)
        while b'\n' in line:
            cut = line.index(b'\n')
            raw = bytes(line[:cut]).rstrip(b'\r')
            del line[:cut + 1]
            if not raw:
                continue
            if len(raw) > 4096:
                raise RuntimeError('oversize serial response line')
            if not raw.startswith(b'PROVISION '):
                continue
            text = raw.removeprefix(b'PROVISION ').decode('ascii', errors='replace')
            if text.startswith('OK GATEWAY '):
                return soak_harness.parse_status_line(text)
    return ('lost', '')


def run_soak(connection, duration, interval, now, sleep, sample_timeout=1.0):
    """Poll for `duration` seconds, one sample per `interval`, and judge the window.

    `now` is a zero-argument clock returning seconds (the runner passes time.monotonic;
    a test passes a fake that advances only when the port is quiet). `sleep` is what the
    runner waits between polls (the test advances the fake clock instead of sleeping).
    Returns the verdict from soak_harness.soak_verdict."""
    samples = []
    start = now()
    while True:
        elapsed = now() - start
        kind, last = sample_status(connection, now(), sample_timeout, now)
        samples.append((elapsed, kind, last))
        if now() - start >= duration:
            break
        sleep(interval)
    return soak_harness.soak_verdict(samples)


def soak_exit(verdict):
    """0 when the window is healthy, 1 otherwise: the soak's result must be readable by
    whatever launches it, so it is an exit code, not just a printed line."""
    return 0 if verdict['ok'] else 1
