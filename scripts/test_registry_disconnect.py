#!/usr/bin/env python3
"""Offline tests for how the registration handler ends.

Run with the Gateway's own interpreter:

    ~/hermes-voice-satellite/.venv/bin/python scripts/test_registry_disconnect.py

Why this exists. A satellite that roams loses its uplink for ordinary reasons, and when the route to
it disappears the kernel fails the read with `OSError: [Errno 113] No route to host` rather than
returning a clean EOF. `RegistrationHandler.run()` guarded its opening write against exactly that
error but left the read loop with only `try/finally`, so the exception escaped the handler task. The
wyoming library starts that task fire-and-forget and its done-callback never calls `.exception()`, so
an ordinary disconnect was reported as `Task exception was never retrieved` at ERROR with a full
traceback. Recovery was never affected — the slot cleared and the loop re-acquired — but the log
described a routine event as a crash.

The checks:

  1  a peer that vanishes mid-read ends the handler cleanly instead of raising
  2  the registration watchdog is not left running on that path
  3  the writer is still closed on that path
  4  a clean EOF still ends the handler normally
  5  cancelling the handler still propagates, so shutdown is not swallowed

Sockets here are real, not mocks of the thing under test: the handler is driven through its own
`run()` against a real stream pair, and only the read failure is injected, because a lost route
cannot be provoked on loopback.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import pathlib
import sys
from typing import Awaitable, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from satellite_gateway.registry import RegistrationHandler, SatelliteRegistry  # noqa: E402

TESTS: list[tuple[str, str, Callable[[], Awaitable[None]]]] = []


def check(number: str, description: str):
    def wrap(fn):
        TESTS.append((number, description, fn))
        return fn
    return wrap


# ---------------------------------------------------------------- helpers

async def real_pair():
    """A real connected stream pair on localhost, plus a way to close each end."""
    loop = asyncio.get_running_loop()
    server_side: asyncio.Future = loop.create_future()

    async def capture(reader, writer):
        if not server_side.done():
            server_side.set_result((reader, writer))

    server = await asyncio.start_server(capture, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client_reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
    reader, writer = await asyncio.wait_for(server_side, 5)
    return server, (reader, writer), (client_reader, client_writer)


class VanishedPeer:
    """The real reader, except the read fails the way a lost route does.

    A clean EOF returns b"" and is a different event; this is the kernel refusing to deliver, which
    is what a satellite behind a travel router produces when its tunnel drops.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.reads = 0

    async def readline(self) -> bytes:
        self.reads += 1
        raise OSError(errno.EHOSTUNREACH, "No route to host")

    async def readexactly(self, n: int) -> bytes:
        return await self._inner.readexactly(n)


class BlockingReader:
    """A reader that never produces anything, so the handler parks until it is cancelled."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def readline(self) -> bytes:
        await self.release.wait()
        return b""

    async def readexactly(self, n: int) -> bytes:
        await self.release.wait()
        return b""


def handler_for(reader, writer) -> RegistrationHandler:
    return RegistrationHandler(
        reader, writer, registry=SatelliteRegistry(), registration_timeout=0.5,
    )


async def settle(task: asyncio.Task | None) -> None:
    """Let a cancelled task finish so its final state can be inspected."""
    if task is None:
        return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(task, 2)


# ---------------------------------------------------------------- checks

@check("1", "a peer that vanishes mid-read ends the handler cleanly instead of raising")
async def t_vanished_peer_does_not_raise() -> None:
    server, (reader, writer), (cr, cw) = await real_pair()
    try:
        vanished = VanishedPeer(reader)
        handler = handler_for(vanished, writer)
        # Before the fix this raised OSError out of run(), which is the whole defect.
        await asyncio.wait_for(handler.run(), 5)
        assert vanished.reads >= 1, "the handler must have actually attempted a read"
    finally:
        for w in (cw, writer):
            with contextlib.suppress(Exception):
                w.close()
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()


@check("2", "the registration watchdog is not left running on that path")
async def t_watchdog_not_leaked() -> None:
    server, (reader, writer), (cr, cw) = await real_pair()
    try:
        handler = handler_for(VanishedPeer(reader), writer)
        await asyncio.wait_for(handler.run(), 5)
        watchdog = handler._registration_watchdog
        assert watchdog is not None, "the handler should have armed its deadline watchdog"
        assert watchdog.done(), "a watchdog left running would linger past the connection"
    finally:
        for w in (cw, writer):
            with contextlib.suppress(Exception):
                w.close()
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()


@check("3", "the writer is still closed on that path")
async def t_writer_closed() -> None:
    server, (reader, writer), (cr, cw) = await real_pair()
    try:
        handler = handler_for(VanishedPeer(reader), writer)
        await asyncio.wait_for(handler.run(), 5)
        assert writer.is_closing(), "the stream must not be left open after the handler ends"
    finally:
        for w in (cw, writer):
            with contextlib.suppress(Exception):
                w.close()
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()


@check("4", "a clean EOF still ends the handler normally")
async def t_clean_eof_still_ends() -> None:
    server, (reader, writer), (cr, cw) = await real_pair()
    try:
        handler = handler_for(reader, writer)
        cw.close()  # a real, polite close from the peer
        with contextlib.suppress(Exception):
            await cw.wait_closed()
        await asyncio.wait_for(handler.run(), 5)
        assert writer.is_closing(), "a clean EOF must still close the stream"
    finally:
        for w in (cw, writer):
            with contextlib.suppress(Exception):
                w.close()
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()


@check("5", "cancelling the handler still propagates, so shutdown is not swallowed")
async def t_cancellation_propagates() -> None:
    server, (reader, writer), (cr, cw) = await real_pair()
    try:
        blocker = BlockingReader()
        handler = handler_for(blocker, writer)
        task = asyncio.create_task(handler.run(), name="handler-under-test")
        await asyncio.sleep(0.05)          # let it reach the read
        assert not task.done(), "the handler should be parked on the read"
        task.cancel()
        raised = False
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.CancelledError:
            raised = True
        assert raised, (
            "cancellation must reach the caller; catching Exception here would swallow shutdown"
        )
        assert task.cancelled(), "the task should finish cancelled, not with a normal return"
    finally:
        for w in (cw, writer):
            with contextlib.suppress(Exception):
                w.close()
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()


# ---------------------------------------------------------------- runner

async def amain() -> int:
    failures = 0
    for number, description, fn in TESTS:
        try:
            await asyncio.wait_for(fn(), 30)
            print(f"  ok    {number:<4} {description}")
        except Exception as exc:  # noqa: BLE001 - a test failure is data, not a crash
            failures += 1
            print(f"  FAIL  {number:<4} {description}")
            print(f"        {type(exc).__name__}: {exc}")
    print(f"\nregistration-disconnect checks: {len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
