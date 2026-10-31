#!/usr/bin/env python3
"""Offline tests that every long-lived voice link can notice a dead peer.

Run with the Gateway's own interpreter:

    ~/hermes-voice-satellite/.venv/bin/python scripts/test_link_liveness.py

Why this exists. An idle TCP connection has no timeout of its own, and neither end of the voice
transport says anything while it waits for the other. A peer that dies without closing — a power cut,
a lost route — therefore leaves a socket that still reads ESTAB, and the surviving end goes on
believing it is connected to something that is gone. Measured on 2026-09-25/26:

* the device dialled in, sat idle, and the Gateway held the socket for hours;
* the Pi was power-cycled, and the Gateway did not notice at all. The accepted socket stayed ESTAB for
  the whole outage and was only cleaned up because the returning device displaced it. Had the Pi
  stayed off, the registration and lease would have been held indefinitely.

The protocol cannot help here: the satellite does not answer application-level pings (measured
2026-09-22, a Ping draws no Pong). Kernel keepalive is the only cheap liveness signal, so it has to be
on for EVERY link, in both directions — the device's outbound socket (already covered by
`test_outbound_runner.py` check 13), the Gateway's dialled client, and the Gateway's accepted registry
sockets. The last of those was the gap.

The checks:

  1  the shared helper turns keepalive on, with the timings the whole project agrees on
  2  an accepted registry connection has keepalive on
  3  the dialled satellite client has keepalive on

Sockets and framing are real throughout; nothing here mocks the thing under test.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import socket
import sys
from typing import Awaitable, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wyoming.event import Event, async_read_event, async_write_event  # noqa: E402

from gateway.net import (  # noqa: E402
    KEEPALIVE_IDLE_SECONDS,
    KEEPALIVE_INTERVAL_SECONDS,
    KEEPALIVE_PROBES,
    enable_tcp_keepalive,
)
from gateway.registry import (  # noqa: E402
    PROTOCOL_VERSION,
    REGISTER_EVENT,
    RegistryServer,
    SatelliteRegistry,
)
from gateway.satellite import SatelliteClient  # noqa: E402

TIMEOUT = 10.0

TESTS: list[tuple[str, str, Callable[[], Awaitable[None]]]] = []


def check(number: str, description: str):
    def wrap(fn):
        TESTS.append((number, description, fn))
        return fn
    return wrap


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def assert_keepalive(sock, label: str) -> None:
    """The one place the expected timings are written down on the Gateway side."""
    assert sock is not None, f"{label}: the stream must expose its socket"
    assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1, (
        f"{label}: an idle link must not survive a vanished peer indefinitely"
    )
    expected = (
        ("TCP_KEEPIDLE", KEEPALIVE_IDLE_SECONDS),
        ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL_SECONDS),
        ("TCP_KEEPCNT", KEEPALIVE_PROBES),
    )
    for name, want in expected:
        option = getattr(socket, name, None)
        if option is not None:
            actual = sock.getsockopt(socket.IPPROTO_TCP, option)
            assert actual == want, f"{label}: {name}={actual}, expected {want}"


class MinimalDevice:
    """A real socket speaking just enough Wyoming to register."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.reader = None
        self.writer = None

    async def register(self, device_id: str = "liveness-1") -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        await asyncio.wait_for(async_read_event(self.reader), TIMEOUT)  # the listener's Describe
        await self._send("info", {"satellite": {"name": "fake", "area": "test-room"}})
        await self._send(REGISTER_EVENT, {
            "device_id": device_id,
            "name": device_id,
            "area": "test-room",
            "firmware_version": "test",
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": ["led", "speaker"],
            "boot_id": "boot-1",
        })
        reply = await asyncio.wait_for(async_read_event(self.reader), TIMEOUT)
        assert reply is not None and "ok" in reply.type, f"registration refused: {reply}"

    async def _send(self, event_type: str, data: dict) -> None:
        await async_write_event(Event(type=event_type, data=data), self.writer)
        await self.writer.drain()

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:  # noqa: BLE001 - closing a fake is not a test outcome
                pass


# ---------------------------------------------------------------- checks

@check("1", "the shared helper turns keepalive on, with the agreed timings")
async def t_helper_sets_options() -> None:
    # A real TCP pair, not socketpair(): AF_UNIX sockets reject SO_KEEPALIVE with EOPNOTSUPP, so a
    # unix pair would test the error path rather than the behaviour.
    accepted: asyncio.Future = asyncio.get_running_loop().create_future()

    async def hold(reader, writer):
        if not accepted.done():
            accepted.set_result(writer)

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client_reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
    peer = await asyncio.wait_for(accepted, TIMEOUT)
    try:
        sock = peer.get_extra_info("socket")
        assert enable_tcp_keepalive(sock, label="test-peer") is True, (
            "the helper should report success on a real TCP socket"
        )
        assert_keepalive(sock, "helper")
    finally:
        for w in (client_writer, peer):
            with contextlib.suppress(Exception):
                w.close()
        server.close()
        await server.wait_closed()


@check("2", "an accepted registry connection has keepalive on")
async def t_accepted_socket_has_keepalive() -> None:
    registry = SatelliteRegistry()
    port = free_port()
    server = RegistryServer(registry, "127.0.0.1", port)
    device = MinimalDevice(port)
    await server.start()
    try:
        await device.register()
        slot = registry.get("liveness-1")
        assert slot is not None and slot.connection is not None, "the device should be registered"
        accepted = slot.connection.writer.get_extra_info("socket")
        assert_keepalive(accepted, "accepted registry socket")
    finally:
        await device.close()
        await server.stop()
        await registry.close_all()


@check("3", "the dialled satellite client has keepalive on")
async def t_dialled_client_has_keepalive() -> None:
    accepted: asyncio.Future = asyncio.get_running_loop().create_future()

    async def hold(reader, writer):
        if not accepted.done():
            accepted.set_result(writer)

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = SatelliteClient("127.0.0.1", port, log_events=False)
    try:
        await client.connect()
        sock = client._client._writer.get_extra_info("socket")
        assert_keepalive(sock, "dialled satellite client")
    finally:
        await client.close()
        if accepted.done():
            accepted.result().close()
        server.close()
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
    print(f"\nlink-liveness checks: {len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
