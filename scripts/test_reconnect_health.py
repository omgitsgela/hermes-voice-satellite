#!/usr/bin/env python3
"""A failed reconnect must not leave the link looking healthy (issue #16).

reconnect() used to clear `_eof` before connecting. When the connect failed the client reported
itself healthy with no reader task, so the loop's `sat.eof` checks never fired and nothing retried
until the next hourly refresh: the device stayed inert for up to an hour after one failed connect.
Run with the bridge venv:

    .venv/bin/python scripts/test_reconnect_health.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge.satellite import SatelliteClient  # noqa: E402

checks = 0
failures = []


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


async def main() -> int:
    # 1. a reconnect whose connect fails must leave the link marked dead, so the loop notices
    client = SatelliteClient("192.0.2.1")   # TEST-NET-1: never routable, and never dialled here
    client._reader_task = None

    async def failing_connect():
        raise ConnectionRefusedError("nothing there")

    client.connect = failing_connect
    raised = None
    try:
        await client.reconnect()
    except ConnectionRefusedError as exc:
        raised = str(exc)
    check("the failure propagates", raised, "nothing there")
    check("link marked dead after a failed reconnect", client.eof, True)
    check("no reader was left behind", client._reader_task, None)

    # 2. a reconnect that succeeds must end up healthy, with the reader running
    client = SatelliteClient("192.0.2.1")

    async def good_connect():
        client._reader_task = "reader"

    client.connect = good_connect
    await client.reconnect()
    check("healthy after a successful reconnect", client.eof, False)
    check("reader present after a successful reconnect", client._reader_task, "reader")

    # 3. a cancelled reconnect is treated as a failure, not as success
    client = SatelliteClient("192.0.2.1")

    async def cancelled_connect():
        raise asyncio.CancelledError()

    client.connect = cancelled_connect
    try:
        await client.reconnect()
    except asyncio.CancelledError:
        pass
    check("link marked dead after a cancelled reconnect", client.eof, True)

    print(f"reconnect health checks: {checks - len(failures)}/{checks} passed")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
