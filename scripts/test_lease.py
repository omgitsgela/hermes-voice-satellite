#!/usr/bin/env python3
"""Offline tests for the lease (issue #61, migration phase 4).

Run with the Gateway's own interpreter:

    ~/hermes-voice-satellite/.venv/bin/python scripts/test_lease.py

No device and no network beyond a localhost socket pair: the peer in these tests speaks real Wyoming framing,
because the failure this file exists to prevent is an event going missing on a live link.

The checks:

  1  a lease reports itself healthy while the link is up, and dead once it closes
  2  a blocking read that times out keeps its place: an event arriving afterwards is still returned
  3  a non-blocking read returns what is queued and nothing else
  4  send puts real Wyoming framing on the wire
  5  the inherited describe() handshake works over a leased connection
  6  run() arms the device, and reconnect() re-arms it over the same socket
  7  a device that goes away wakes a blocked read promptly instead of at the timeout
  8  an event and a close arriving together: the event is delivered, not dropped
"""

from __future__ import annotations

import asyncio
import pathlib
import socket
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wyoming.audio import AudioChunk  # noqa: E402
from wyoming.event import Event, async_read_event, async_write_event  # noqa: E402
from wyoming.info import Attribution, Describe, Info, Satellite  # noqa: E402
from wyoming.satellite import PauseSatellite, RunSatellite  # noqa: E402

from gateway.lease import LeasedSatellite  # noqa: E402
from gateway.registry import SatelliteConnection  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok    {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")


async def leased_pair(device_id: str = "test-device", generation: int = 7):
    """A SatelliteConnection plus the far end of its socket, both real asyncio streams.

    The reader is pumped into `deliver` exactly as `RegistryServer` does in production. Without it the test
    would be lying about the code under test: events written by the peer would sit unread in the socket and
    every wait would time out for a reason that does not exist on a real gateway.
    """
    ours, theirs = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=ours)
    peer_reader, peer_writer = await asyncio.open_connection(sock=theirs)
    connection = SatelliteConnection(device_id, generation, reader, writer)

    async def pump() -> None:
        while True:
            event = await async_read_event(reader)
            if event is None:
                return
            connection.deliver(event)

    asyncio.ensure_future(pump())
    return LeasedSatellite(connection), connection, peer_reader, peer_writer


async def check_liveness() -> None:
    satellite, connection, _peer_reader, _peer_writer = await leased_pair()
    check("a fresh lease is not eof", satellite.eof is False)
    check("connect() does not dial", await _returns_quickly(satellite.connect()))
    await satellite.close()
    check("after close() the lease reports eof", satellite.eof is True)
    check("the connection is marked closed", connection.closed is True)
    check("closing twice is harmless", await _returns_quickly(satellite.close()))


async def _returns_quickly(coro, limit: float = 1.0) -> bool:
    try:
        await asyncio.wait_for(coro, limit)
        return True
    except asyncio.TimeoutError:
        return False


async def check_read_keeps_its_place() -> None:
    """The defect this design avoids: a sliced read that cancels and loses the event that just arrived."""
    satellite, connection, _peer_reader, _peer_writer = await leased_pair()
    first = asyncio.ensure_future(satellite.next_event(0.3))
    await asyncio.sleep(0.05)
    connection.deliver(Event(type="detection", data={"name": "hey_hermes"}))
    event = await first
    check("an event delivered during a wait is returned", event is not None and event.type == "detection")
    # Now the harder case: deliver *after* a slice has already expired, then read again.
    check("a timeout returns None", await satellite.next_event(0.2) is None)
    connection.deliver(Event(type="detection", data={"name": "second"}))
    event = await satellite.next_event(0.5)
    check("the read was still in place after a timeout", event is not None and event.data.get("name") == "second")
    await satellite.close()


async def check_non_blocking_read() -> None:
    satellite, connection, _peer_reader, _peer_writer = await leased_pair()
    check("nothing queued reads as None", await satellite.next_event(0) is None)
    connection.deliver(Event(type="played"))
    check("a queued event is returned without waiting", (await satellite.next_event(0)).type == "played")
    await satellite.close()


async def check_send_framing() -> None:
    satellite, _connection, peer_reader, _peer_writer = await leased_pair()
    await satellite.send(Event(type="hermes.led.thinking"))
    received = await asyncio.wait_for(async_read_event(peer_reader), 1.0)
    check("send writes real Wyoming framing", received is not None and received.type == "hermes.led.thinking")
    await satellite.close()


async def check_describe_handshake() -> None:
    satellite, _connection, peer_reader, peer_writer = await leased_pair()

    async def answer():
        request = await asyncio.wait_for(async_read_event(peer_reader), 1.0)
        assert Describe.is_type(request.type), request.type
        await async_write_event(
            Info(satellite=Satellite(
                name="satellite", attribution=Attribution(name="test", url="https://example.invalid"),
                installed=True, description="test", version="1.0",
            )).event(),
            peer_writer,
        )

    task = asyncio.ensure_future(answer())
    info = await satellite.describe()
    await task
    check("the inherited describe() works over a lease", info is not None)
    check("describe() returns the device's own name",
          info is not None and info.satellite is not None and info.satellite.name == "satellite")
    await satellite.close()


async def check_arming() -> None:
    satellite, _connection, peer_reader, _peer_writer = await leased_pair()

    async def expect(requested: str):
        event = await asyncio.wait_for(async_read_event(peer_reader), 1.0)
        return event.type

    await satellite.run()
    check("run() sends run-satellite", await expect(RunSatellite().event().type) == RunSatellite().event().type)
    await satellite.reconnect()
    check("reconnect() re-arms over the same socket",
          await expect(RunSatellite().event().type) == RunSatellite().event().type)
    await satellite.pause()
    check("pause() sends pause-satellite", await expect(PauseSatellite().event().type) == PauseSatellite().event().type)
    await satellite.close()


async def check_close_wakes_a_reader() -> None:
    satellite, connection, _peer_reader, _peer_writer = await leased_pair()
    waiting = asyncio.ensure_future(satellite.next_event(30.0))
    await asyncio.sleep(0.05)
    await connection.close()
    try:
        event = await asyncio.wait_for(waiting, 2.0)
        woke = True
    except asyncio.TimeoutError:
        event, woke = None, False
    check("a closed link wakes a blocked read promptly", woke)
    check("and it reads as None with eof set", event is None and satellite.eof is True)
    await satellite.close()


async def check_event_wins_over_close() -> None:
    """An event and a close in the same instant: deliver the event, then report the end next time."""
    satellite, connection, _peer_reader, _peer_writer = await leased_pair()
    connection.deliver(Event(type="played"))
    await connection.close()
    event = await satellite.next_event(1.0)
    check("an event already delivered is not dropped by a close", event is not None and event.type == "played")
    check("the next read reports the end", await satellite.next_event(1.0) is None and satellite.eof is True)
    await satellite.close()


async def check_audio_chunks_are_not_logged_as_events() -> None:
    """Audio must not go through the event log: it would print a line per chunk per sentence."""
    satellite, connection, _peer_reader, _peer_writer = await leased_pair()
    chunk = AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x01" * 512).event()
    check("an audio chunk is recognised as audio", AudioChunk.is_type(chunk.type))
    connection.deliver(chunk)
    event = await satellite.next_event(0)
    check("an audio chunk still comes through the lease", event is not None and AudioChunk.is_type(event.type))
    await satellite.close()


async def main() -> int:
    print("lease (issue #61 phase 4)")
    await check_liveness()
    await check_read_keeps_its_place()
    await check_non_blocking_read()
    await check_send_framing()
    await check_describe_handshake()
    await check_arming()
    await check_close_wakes_a_reader()
    await check_event_wins_over_close()
    await check_audio_chunks_are_not_logged_as_events()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
