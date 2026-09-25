#!/usr/bin/env python3
"""Offline checks for the device-side outbound runner (issue #61, migration phase 3).

Run with the bridge's interpreter, which is where `wyoming` lives:

    ~/hermes-voice-bridge/.venv/bin/python scripts/test_outbound_runner.py

A **fake bridge** is a real socket server playing the host's half of the contract: it sends `describe`,
reads `info` and `hermes.register`, and can accept, refuse, drop the connection, or stop reading. No
device and no hardware are involved, which is the point: phase 3 is testable while the satellite is away.

Checks:

  1  the handshake registers, once, and readiness is reported only after acceptance
  2  events flow both ways after registration
  3  a refusal is surfaced with its reason and is not retried on the short timer
  4  a dropped connection reconnects and re-registers
  5  ping is answered with pong, so an idle link stays honest
  6  the backoff grows with consecutive failures and is capped
 6b  an accept-then-drop bridge accumulates backoff rather than retrying at the base rate (#63)
  7  a stalled write fails the link instead of hanging the device
  8  the bootstrap configuration is exactly what the device needs
  9  a stale write failure does not tear down a newer connection (#64)
"""

from __future__ import annotations

import asyncio
import pathlib
import socket
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wyoming.event import Event, async_read_event, async_write_event  # noqa: E402

from pi.outbound_satellite import (  # noqa: E402
    REGISTER_EVENT,
    REGISTER_OK_EVENT,
    REGISTER_REFUSED_EVENT,
    BridgeLink,
    LinkConfig,
    load_config_from_env,
)

TIMEOUT = 5.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def write_event(writer: asyncio.StreamWriter, event: Event) -> None:
    await async_write_event(event, writer)
    await writer.drain()


class FakeBridge:
    """The host's half of the contract, with the failure modes a real bridge has."""

    def __init__(self, *, refuse_reason: str | None = None, drop_on_register: bool = False,
                 drop_every_register: bool = False, stop_reading: bool = False) -> None:
        self.refuse_reason = refuse_reason
        self.drop_on_register = drop_on_register
        self.drop_every_register = drop_every_register
        self.stop_reading = stop_reading
        self.port = free_port()
        self.connections = 0
        self.connection_times: list[float] = []
        self.registrations: list[dict] = []
        self.received: list[Event] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.registered = asyncio.Event()
        self._drops = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for writer in self.writers:
            writer.close()

    async def push(self, event: Event) -> None:
        await write_event(self.writers[-1], event)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.connection_times.append(asyncio.get_running_loop().time())
        self.writers.append(writer)
        await write_event(writer, Event(type="describe"))
        while True:
            event = await async_read_event(reader)
            if event is None:
                return
            self.received.append(event)
            if event.type == "info":
                continue
            if event.type == REGISTER_EVENT:
                self.registrations.append(dict(event.data))
                if self.refuse_reason:
                    await write_event(
                        writer, Event(type=REGISTER_REFUSED_EVENT, data={"reason": self.refuse_reason})
                    )
                    writer.close()
                    return
                await write_event(
                    writer, Event(type=REGISTER_OK_EVENT, data={"device_id": event.data.get("device_id")})
                )
                self.registered.set()
                if self.drop_every_register:
                    # A bridge that accepts the handshake and then closes, every time: the flapping case
                    # filed as #63.
                    writer.close()
                    return
                if self.drop_on_register and self._drops == 0:
                    # Drop the first accepted connection, the way a bridge restarted mid-session would.
                    self._drops += 1
                    writer.close()
                    return
                if self.stop_reading:
                    # A bridge that stops reading: the device's writes must fail, not hang.
                    await asyncio.sleep(3600)
                    return
                continue


def config_for(port: int, **overrides) -> LinkConfig:
    base = dict(
        device_id="kitchen-1",
        host="127.0.0.1",
        port=port,
        name="Kitchen",
        area="kitchen",
        firmware_version="test",
        capabilities=("led", "speaker", "microphone"),
        backoff_base=0.05,
        backoff_max=0.4,
        refused_backoff=30.0,
        connect_timeout=2.0,
        handshake_timeout=2.0,
        write_timeout=2.0,
    )
    base.update(overrides)
    return LinkConfig(**base)


class LinkRunner:
    """Runs a link as a task and cleans it up."""

    def __init__(self, link: BridgeLink) -> None:
        self.link = link
        self.events: list[Event] = []
        self.registered_payloads: list[dict] = []
        self.disconnects: list[str] = []
        link.on_event = self._on_event
        link.on_registered = self._on_registered
        link.on_disconnected = self._on_disconnected
        self._task: asyncio.Task | None = None

    async def _on_event(self, event: Event) -> None:
        self.events.append(event)

    async def _on_registered(self, payload: dict) -> None:
        self.registered_payloads.append(payload)

    async def _on_disconnected(self, reason: str) -> None:
        self.disconnects.append(reason)

    async def start(self) -> None:
        self._task = asyncio.create_task(self.link.run())

    async def stop(self) -> None:
        await self.link.stop()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


TESTS: list[tuple[str, str, object]] = []


def check(number: str, description: str):
    def wrap(fn):
        TESTS.append((number, description, fn))
        return fn
    return wrap


@check("1", "the handshake registers once, and readiness is reported only after acceptance")
async def t_handshake() -> None:
    bridge = FakeBridge()
    await bridge.start()
    link = BridgeLink(config_for(bridge.port))
    runner = LinkRunner(link)
    # What the world looked like at the moment readiness was reported. A callback fired at TCP connect
    # would show zero registrations here, which is the failure this check exists to catch.
    observed: list[tuple[int, bool, bool, dict]] = []

    async def observe(payload: dict) -> None:
        observed.append((len(bridge.registrations), link.registered, link.connected, dict(payload)))

    link.on_registered = observe
    try:
        await runner.start()
        await asyncio.wait_for(bridge.registered.wait(), TIMEOUT)
        await asyncio.sleep(0.1)
        assert bridge.connections == 1, f"expected one connection, saw {bridge.connections}"
        payload = bridge.registrations[0]
        assert payload["device_id"] == "kitchen-1"
        assert payload["area"] == "kitchen"
        assert payload["protocol_version"] == 1
        assert link.registered and link.connected
        assert len(observed) == 1, "readiness must be reported exactly once"
        registrations, registered_flag, connected_flag, reported = observed[0]
        assert registrations == 1, "readiness must not be reported before the bridge processed the register"
        assert registered_flag and connected_flag
        assert reported["device_id"] == "kitchen-1"
    finally:
        await runner.stop()
        await bridge.stop()


@check("2", "events flow both ways after registration")
async def t_both_directions() -> None:
    bridge = FakeBridge()
    await bridge.start()
    link = BridgeLink(config_for(bridge.port))
    runner = LinkRunner(link)
    try:
        await runner.start()
        await asyncio.wait_for(bridge.registered.wait(), TIMEOUT)
        await bridge.push(Event(type="led-idle"))
        await asyncio.sleep(0.15)
        assert [e.type for e in runner.events] == ["led-idle"], f"saw {[e.type for e in runner.events]}"
        assert await link.send(Event(type="detection", data={"name": "hey_hermes"}))
        await asyncio.sleep(0.15)
        assert any(e.type == "detection" for e in bridge.received), "the bridge must receive the detection"
    finally:
        await runner.stop()
        await bridge.stop()


@check("3", "a refusal surfaces its reason and is not retried on the short timer")
async def t_refusal() -> None:
    bridge = FakeBridge(refuse_reason="no recognised credential")
    await bridge.start()
    link = BridgeLink(config_for(bridge.port))
    runner = LinkRunner(link)
    try:
        await runner.start()
        await asyncio.sleep(0.5)
        assert link.stats.refusals == 1, f"expected one refusal, saw {link.stats.refusals}"
        assert "no recognised credential" in link.stats.last_error
        assert not link.registered
        assert bridge.connections == 1, (
            f"a refusal must not be retried on the short timer, saw {bridge.connections} connections"
        )
        # The refusal path waits on the fixed long timer rather than the jittered reconnect backoff, so
        # last_backoff staying at zero is the correct expectation here. The connection count above is what
        # proves there was no short retry.
        assert link.stats.last_backoff == 0.0, "a refusal must not consume the jittered backoff"
    finally:
        await runner.stop()
        await bridge.stop()


@check("4", "a dropped connection reconnects and re-registers")
async def t_reconnect() -> None:
    bridge = FakeBridge(drop_on_register=True)
    await bridge.start()
    link = BridgeLink(config_for(bridge.port))
    runner = LinkRunner(link)
    try:
        await runner.start()
        deadline = asyncio.get_running_loop().time() + 6
        while asyncio.get_running_loop().time() < deadline and bridge.connections < 2:
            await asyncio.sleep(0.1)
        assert bridge.connections >= 2, f"the device must reconnect, saw {bridge.connections} connections"
        assert len(bridge.registrations) >= 2, "each new connection must register again"
        assert runner.disconnects, "the dropped connection must be reported to the caller"
    finally:
        await runner.stop()
        await bridge.stop()


@check("5", "ping is answered with pong")
async def t_ping_pong() -> None:
    bridge = FakeBridge()
    await bridge.start()
    link = BridgeLink(config_for(bridge.port))
    runner = LinkRunner(link)
    try:
        await runner.start()
        await asyncio.wait_for(bridge.registered.wait(), TIMEOUT)
        await bridge.push(Event(type="ping"))
        await asyncio.sleep(0.2)
        assert any(e.type == "pong" for e in bridge.received), "an idle link must answer ping"
        assert not any(e.type == "ping" for e in runner.events), "ping must not reach the event sink"
    finally:
        await runner.stop()
        await bridge.stop()


@check("6", "the backoff grows with consecutive failures and is capped")
async def t_backoff() -> None:
    link = BridgeLink(config_for(free_port(), backoff_base=0.5, backoff_max=4.0))
    delays = [link._next_backoff() for _ in range(8)]
    assert all(d <= 4.0 for d in delays), f"backoff must be capped: {delays}"
    assert delays[0] <= 0.5, f"the first delay must be small: {delays[0]}"
    assert max(delays) >= 2.0, f"the cap must actually grow: {delays}"
    # A reset only happens on a successful registration, which is what stops a flapping link settling into
    # a tight retry loop.
    link._backoff_index = 0
    assert link._next_backoff() <= 0.5


@check("7", "a stalled write fails the link instead of hanging the device")
async def t_stalled_write() -> None:
    bridge = FakeBridge(stop_reading=True)
    await bridge.start()
    link = BridgeLink(config_for(bridge.port, write_timeout=0.5))
    runner = LinkRunner(link)
    try:
        await runner.start()
        await asyncio.wait_for(bridge.registered.wait(), TIMEOUT)
        payload = b"\x00" * (1024 * 1024)
        failed = False
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            if not await link.send(Event(type="audio-chunk", payload=payload)):
                failed = True
                break
        assert failed, "a bridge that stops reading must fail the write, not hang the device"
        assert link.stats.write_failures == 1, f"saw {link.stats.write_failures} write failures"
        assert not link.connected, "the link must be torn down so the reconnect loop can run"
    finally:
        await runner.stop()
        await bridge.stop()


@check("8", "the bootstrap configuration is exactly what the device needs")
async def t_config_from_env() -> None:
    config = load_config_from_env({
        "VOICE_DEVICE_ID": "kitchen-1",
        "VOICE_BRIDGE_HOST": "bridge.example",
        "VOICE_BRIDGE_PORT": "443",
        "VOICE_DEVICE_AREA": "kitchen",
    })
    assert config.device_id == "kitchen-1"
    assert config.host == "bridge.example" and config.port == 443
    assert config.ssl_context is None, "a plain local connection must not build a TLS context"
    try:
        load_config_from_env({})
        raise AssertionError("a device with no identity or endpoint must refuse to start")
    except SystemExit:
        pass


@check("6b", "an accept-then-drop bridge accumulates backoff instead of retrying at the base interval")
async def t_flapping_backoff() -> None:
    # Filed as #63: the reset used to happen on every handshake, so a bridge that accepts and immediately
    # closes produced a tight retry loop forever. This drives the real run loop rather than calling
    # _next_backoff directly.
    bridge = FakeBridge(drop_every_register=True)
    await bridge.start()
    link = BridgeLink(config_for(bridge.port, backoff_base=0.05, backoff_max=1.0, healthy_after=30.0))
    runner = LinkRunner(link)
    try:
        await runner.start()
        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline and link._backoff_index < 4:
            await asyncio.sleep(0.05)
        assert bridge.connections >= 4, f"expected several attempts, saw {bridge.connections}"
        assert link._backoff_index >= 4, (
            f"the backoff must grow across a flapping link, index={link._backoff_index}"
        )
        gaps = [b - a for a, b in zip(bridge.connection_times, bridge.connection_times[1:])]
        assert len(gaps) >= 3, f"need a few gaps to compare, got {gaps}"
        assert gaps[-1] > gaps[0], f"attempts must space out, gaps={[round(g, 3) for g in gaps]}"
        assert link.stats.registrations >= 4, "each attempt did register; that is what made it look healthy"
    finally:
        await runner.stop()
        await bridge.stop()


@check("9", "a stale write failure does not tear down a newer connection")
async def t_stale_write_teardown() -> None:
    # Filed as #64: send() saved a local writer reference but tore down unconditionally, so a write that
    # failed on a superseded socket closed the replacement the run loop had already installed.
    bridge = FakeBridge(stop_reading=True)
    await bridge.start()
    link = BridgeLink(config_for(bridge.port, write_timeout=0.5))
    runner = LinkRunner(link)
    try:
        await runner.start()
        await asyncio.wait_for(bridge.registered.wait(), TIMEOUT)
        old_writer = link.writer
        assert old_writer is not None
        # A write that cannot complete, because the bridge has stopped reading.
        stale = asyncio.create_task(
            link.send(Event(type="audio-chunk", payload=b"\x00" * (8 * 1024 * 1024)))
        )
        await asyncio.sleep(0.1)
        # Meanwhile the run loop reconnects and installs a new generation.
        reader2, new_writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        link.writer, link.reader, link.connected = new_writer, reader2, True
        assert not await stale, "the stale write must fail"
        assert link.writer is new_writer, "the live connection must still be installed"
        assert not new_writer.is_closing(), "a stale failure must not close the live connection"
        assert link.connected, "the link must still be up after a superseded write fails"
        new_writer.close()
    finally:
        await runner.stop()
        await bridge.stop()


# ---------------------------------------------------------------- runner

async def amain() -> int:
    failures = 0
    for number, description, fn in TESTS:
        try:
            await asyncio.wait_for(fn(), 60)
            print(f"  ok    {number:<4} {description}")
        except Exception as exc:  # noqa: BLE001 - a failing check is data
            failures += 1
            print(f"  FAIL  {number:<4} {description}")
            print(f"        {type(exc).__name__}: {exc}")
    print(f"\noutbound runner checks: {len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
