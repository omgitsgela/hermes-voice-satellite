#!/usr/bin/env python3
"""Registry egress writes are bounded, so a stalled TCP window cannot park senders forever (#166).

Contributor fault: on a Windows/USB installation an unbounded drain under the connection's
write lock parked the session pump that reads mic audio - the host-side "nothing said in
30s" deafness. Their fix: a write deadline around the egress write. This test drives the
registry's own SatelliteConnection.send against a writer whose drain never returns, and
demands the connection be declared dead within the timeout instead of hanging.

    .venv/bin/python scripts/test_egress_write_deadline.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from satellite_gateway.registry import SatelliteConnection  # noqa: E402
from wyoming.event import Event  # noqa: E402

checks = 0
failures = []


def check(condition: bool, message: str) -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")


class StubWriter:
    """A StreamWriter stand-in. drain() either returns or wedges, like a stalled TCP window."""

    def __init__(self, *, wedge_drain: bool) -> None:
        self.wedge = wedge_drain
        self.closed = False
        self.transport_closed = False
        self.bytes_written = 0

    def writelines(self, lines) -> None:
        self.bytes_written += sum(len(line) for line in lines)

    def write(self, data: bytes) -> None:
        self.bytes_written += len(data)

    async def drain(self) -> None:
        if self.wedge:
            await asyncio.sleep(3600)

    def close(self) -> None:
        self.closed = True

    def transport(self):
        class _T:
            def is_closing(self) -> bool:
                return True

            def close(self) -> None:
                self.closed = True

        t = _T()
        t.close = lambda: setattr(self, "transport_closed", True)
        return t

    def get_extra_info(self, name, default=None):
        return default


async def run_case(*, wedge: bool) -> tuple[SatelliteConnection, StubWriter, bool, float]:
    import time

    writer = StubWriter(wedge_drain=wedge)
    reader = asyncio.StreamReader()
    connection = SatelliteConnection("test-device", 1, reader, writer)  # type: ignore[arg-type]
    started = time.monotonic()
    result = await connection.send(Event(type="run-satellite", data={"x": 1}))
    elapsed = time.monotonic() - started
    return connection, writer, result, elapsed


async def main() -> int:
    import satellite_gateway.registry as registry

    original = getattr(registry, "WRITE_TIMEOUT_SECONDS", None)
    registry.WRITE_TIMEOUT_SECONDS = 0.5
    try:
        connection, writer, result, elapsed = await run_case(wedge=True)
        check(result is False,
              "send against a wedged writer must return False, not True")
        check(elapsed < 4.0,
              f"send against a wedged writer must return within the deadline, took {elapsed:.1f}s "
              "(an unbounded drain parks the caller and, through it, the session pump - issue #166)")
        check(connection.closed, "a timed-out write must mark the connection closed")
        check(connection.closed_event.is_set(),
              "a timed-out write must wake everything waiting on closed_event")
        check(writer.closed or writer.transport_closed,
              "a timed-out write must close the transport")
        after = await connection.send(Event(type="run-satellite"))
        check(after is False, "send after closure must be False")

        # Healthy path: a writer that drains still succeeds and stays open.
        connection2, writer2, result2, _ = await run_case(wedge=False)
        check(result2 is True, "send against a healthy writer must return True")
        check(not connection2.closed, "a successful send must not close the connection")

        # A typed waiter (the Played path) must be released, not left holding a future.
        connection3 = None
        writer3 = StubWriter(wedge_drain=True)
        reader3 = asyncio.StreamReader()
        connection3 = SatelliteConnection("test-device", 1, reader3, writer3)  # type: ignore[arg-type]
        waiter = asyncio.create_task(connection3.wait_for("played"))
        await asyncio.sleep(0)
        await connection3.send(Event(type="run-satellite"))
        await asyncio.sleep(0)
        check(waiter.cancelled(),
              "a timed-out egress must cancel pending typed waiters like the ConnectionError path does")
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    finally:
        if original is None:
            try:
                del registry.WRITE_TIMEOUT_SECONDS
            except AttributeError:
                pass
        else:
            registry.WRITE_TIMEOUT_SECONDS = original

    print(f"{checks - len(failures)}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
