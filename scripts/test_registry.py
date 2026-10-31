#!/usr/bin/env python3
"""Offline tests for the device registry (issue #61, migration phase 2).

Run with the bridge's own interpreter:

    ~/hermes-voice-bridge/.venv/bin/python scripts/test_registry.py

No device, no network beyond localhost, no mocks of the thing under test: fake devices are real sockets
speaking real Wyoming framing to a real listener, because the failures worth catching here are framing,
ordering and lifecycle failures.

The checks map to the migration's acceptance criteria:

  1  the listener is healthy with zero devices, and one device can register
  2  an identity that disagrees with the authenticated credential is refused
  3  an unknown credential is refused rather than believed
  4  a duplicate connection follows the configured policy
  5  a stale handler's cleanup cannot remove a newer connection (the ABA guard)
  6  a slot and its generation survive a reconnect
  7  typed waiters and the streaming consumer do not steal each other's events
  8  the ingress queue is bounded, and overflow is counted rather than silent
  9  playback locks are per device, not per process
 10  a device that never registers is dropped at the deadline
 11  the registration handshake is refused for an unsupported protocol version
 12  the credential file fails closed: absent needs an explicit opt-in, broken always refuses (#62)
 13  registration validation rejects what the contract does not allow
 14  configuration reports are handled out of band and never leak into audio
"""

from __future__ import annotations

import asyncio
import pathlib
import socket
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wyoming.event import Event, async_read_event, async_write_event  # noqa: E402

from bridge.registry import (  # noqa: E402
    CONFIG_REPORTED_EVENT,
    PROTOCOL_VERSION,
    REGISTER_EVENT,
    REGISTER_OK_EVENT,
    REGISTER_REFUSED_EVENT,
    DeviceIdentity,
    DuplicatePolicy,
    IngressLimits,
    RegistryServer,
    SatelliteConnection,
    SatelliteRegistry,
)

TIMEOUT = 3.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeDevice:
    """A fake satellite: a real socket, real framing, no mic and no LED service."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.describe_seen = False

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        first = await asyncio.wait_for(async_read_event(self.reader), TIMEOUT)
        # The listener asks Describe first, the standard way, and the fake device answers with Info.
        self.describe_seen = bool(first and first.type == "describe")
        await self.send("info", {"satellite": {"name": "fake", "area": "test-room"}})

    async def send(self, event_type: str, data: dict | None = None, payload: bytes | None = None) -> None:
        assert self.writer is not None
        await async_write_event(Event(type=event_type, data=data or {}, payload=payload), self.writer)
        await self.writer.drain()

    async def register(self, device_id: str, **overrides) -> Event | None:
        payload = {
            "device_id": device_id,
            "name": overrides.pop("name", device_id),
            "area": overrides.pop("area", "test-room"),
            "firmware_version": overrides.pop("firmware_version", "test"),
            "protocol_version": overrides.pop("protocol_version", PROTOCOL_VERSION),
            "capabilities": overrides.pop("capabilities", ["led", "speaker"]),
            "boot_id": overrides.pop("boot_id", "boot-1"),
        }
        payload.update(overrides)
        await self.send(REGISTER_EVENT, payload)
        return await self.recv()

    async def recv(self, timeout: float = TIMEOUT) -> Event | None:
        assert self.reader is not None
        return await asyncio.wait_for(async_read_event(self.reader), timeout)

    async def closed_by_server(self, timeout: float = TIMEOUT) -> bool:
        """True when the server closed this socket (reads return None at EOF)."""
        try:
            return (await self.recv(timeout)) is None
        except (asyncio.TimeoutError, ConnectionError):
            return False

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass


class Harness:
    """A registry, a listener on a free localhost port, and a way to close it."""

    def __init__(self, **registry_kwargs) -> None:
        self.registry = SatelliteRegistry(**registry_kwargs)
        self.port = free_port()
        self.server = RegistryServer(self.registry, "127.0.0.1", self.port)
        self.devices: list[FakeDevice] = []

    async def start(self, **server_kwargs) -> None:
        if server_kwargs:
            self.server = RegistryServer(self.registry, "127.0.0.1", self.port, **server_kwargs)
        await self.server.start()

    async def device(self) -> FakeDevice:
        dev = FakeDevice(self.port)
        await dev.connect()
        self.devices.append(dev)
        return dev

    async def stop(self) -> None:
        for dev in self.devices:
            await dev.close()
        await self.server.stop()
        await self.registry.close_all()


TESTS: list[tuple[str, str]] = []


def check(number: str, description: str):
    def wrap(fn):
        TESTS.append((number, description, fn))
        return fn
    return wrap


# ---------------------------------------------------------------- checks

@check("1", "the listener is healthy with zero devices, and one device registers")
async def t_zero_then_one() -> None:
    h = Harness()
    await h.start()
    try:
        assert len(h.registry) == 0, "an empty registry should be a normal state"
        assert h.registry.connected_devices() == []
        dev = await h.device()
        assert dev.describe_seen, "the listener should ask Describe first"
        reply = await dev.register("kitchen-1")
        assert reply is not None and reply.type == REGISTER_OK_EVENT, f"unexpected reply {reply}"
        assert len(h.registry) == 1
        slot = h.registry.get("kitchen-1")
        assert slot is not None and slot.ready
        assert slot.identity is not None and slot.identity.area == "test-room"
        assert slot.identity.capabilities == ("led", "speaker")
        assert h.registry.connected_devices() == ["kitchen-1"]
    finally:
        await h.stop()


@check("2", "an identity that disagrees with the authenticated credential is refused")
async def t_spoof_refused() -> None:
    h = Harness()
    # Any peer presents the same fingerprint over plain localhost, so the lookup maps it to one device and
    # a payload claiming another id must be refused.
    await h.start(credential_lookup=lambda fingerprint: "kitchen-1")
    try:
        dev = await h.device()
        reply = await dev.register("study-1")
        assert reply is not None and reply.type == REGISTER_REFUSED_EVENT, f"unexpected reply {reply}"
        assert "does not match" in reply.data.get("reason", "")
        assert len(h.registry) == 0, "a refused device must not create a slot"
    finally:
        await h.stop()


@check("3", "an unknown credential is refused rather than believed")
async def t_unknown_credential_refused() -> None:
    h = Harness()
    await h.start(credential_lookup=lambda fingerprint: None)
    try:
        dev = await h.device()
        reply = await dev.register("kitchen-1")
        assert reply is not None and reply.type == REGISTER_REFUSED_EVENT
        assert "credential" in reply.data.get("reason", "")
        assert len(h.registry) == 0
    finally:
        await h.stop()


@check("4a", "with the reject policy a duplicate connection is refused and the first survives")
async def t_duplicate_reject() -> None:
    h = Harness(duplicate_policy=DuplicatePolicy.REJECT)
    await h.start()
    try:
        first = await h.device()
        assert (await first.register("kitchen-1")).type == REGISTER_OK_EVENT
        second = await h.device()
        reply = await second.register("kitchen-1")
        assert reply is not None and reply.type == REGISTER_REFUSED_EVENT
        slot = h.registry.get("kitchen-1")
        assert slot is not None and slot.connection.generation == 1, "the first connection must stay current"
    finally:
        await h.stop()


@check("4b", "with the replace policy the newer connection wins and the older socket is closed")
async def t_duplicate_replace() -> None:
    h = Harness(duplicate_policy=DuplicatePolicy.REPLACE)
    await h.start()
    try:
        first = await h.device()
        assert (await first.register("kitchen-1")).type == REGISTER_OK_EVENT
        generation_one = h.registry.get("kitchen-1").connection.generation
        second = await h.device()
        assert (await second.register("kitchen-1")).type == REGISTER_OK_EVENT
        slot = h.registry.get("kitchen-1")
        assert slot.connection.generation == generation_one + 1, "the replacement must be a new generation"
        assert await first.closed_by_server(), "the replaced socket must be closed by the host"
    finally:
        await h.stop()


@check("5", "a stale handler's cleanup cannot remove a newer connection")
async def t_aba_guard() -> None:
    registry = SatelliteRegistry(duplicate_policy=DuplicatePolicy.REPLACE)

    class DummyWriter:
        def close(self): pass
        def get_extra_info(self, name, default=None): return ("127.0.0.1", 1)

    old = SatelliteConnection("kitchen-1", 1, None, DummyWriter())
    new = SatelliteConnection("kitchen-1", 2, None, DummyWriter())
    identity = DeviceIdentity(device_id="kitchen-1")
    await registry.register(identity, old)
    await registry.register(identity, new)
    assert registry.get("kitchen-1").connection is new
    # The old handler finally runs its cleanup, after the replacement.
    assert registry.unregister("kitchen-1", 1) is False, "stale cleanup must not remove the live connection"
    assert registry.get("kitchen-1").connection is new, "the replacement must still be current"
    # The current generation's own cleanup does remove it.
    assert registry.unregister("kitchen-1", 2) is True
    assert registry.get("kitchen-1").connection is None


@check("6", "a slot, its identity and its generation survive a reconnect")
async def t_reconnect_keeps_slot() -> None:
    h = Harness()
    await h.start()
    try:
        first = await h.device()
        assert (await first.register("kitchen-1", area="kitchen")).type == REGISTER_OK_EVENT
        slot = h.registry.get("kitchen-1")
        slot.session_id = "session-abc"
        generation_one = slot.connection.generation
        await first.close()
        await asyncio.sleep(0.15)
        assert h.registry.get("kitchen-1") is slot, "the slot must survive a dropped socket"

        second = await h.device()
        assert (await second.register("kitchen-1", area="kitchen")).type == REGISTER_OK_EVENT
        assert slot.connection.generation == generation_one + 1
        assert slot.identity.area == "kitchen"
        assert slot.session_id == "session-abc", "per-device session state must survive a reconnect"
    finally:
        await h.stop()


@check("7", "typed waiters and the streaming consumer do not steal each other's events")
async def t_waiter_and_stream() -> None:
    h = Harness()
    await h.start()
    try:
        dev = await h.device()
        assert (await dev.register("kitchen-1")).type == REGISTER_OK_EVENT
        connection = h.registry.get("kitchen-1").connection

        streamed: list[Event] = []

        async def consume() -> None:
            while True:
                event = await connection.get()
                streamed.append(event)
                if len(streamed) == 2:
                    return

        consumer = asyncio.create_task(consume())
        await dev.send("audio-chunk", {}, b"\x00\x01" * 8)
        await asyncio.sleep(0.05)
        waiter = asyncio.create_task(connection.wait_for("played", timeout=TIMEOUT))
        await dev.send("played")
        played = await waiter
        await dev.send("audio-chunk", {}, b"\x02\x03" * 8)
        await asyncio.wait_for(consumer, TIMEOUT)

        assert played is not None and played.type == "played", "the waiter must receive its own event"
        assert [e.type for e in streamed] == ["audio-chunk", "audio-chunk"], (
            f"the consumer must still see the audio, and not the waiter's event: {[e.type for e in streamed]}"
        )
    finally:
        await h.stop()


@check("8", "the ingress queue is bounded and overflow is counted")
async def t_bounded_queue() -> None:
    h = Harness(limits=IngressLimits(max_events=2, max_bytes=64))
    await h.start()
    try:
        dev = await h.device()
        assert (await dev.register("kitchen-1")).type == REGISTER_OK_EVENT
        connection = h.registry.get("kitchen-1").connection
        for _ in range(8):
            await dev.send("audio-chunk", {}, b"\x00" * 16)
        await asyncio.sleep(0.2)
        assert connection.queued <= 2, f"the queue must stay bounded, saw {connection.queued}"
        assert connection.queued_bytes <= 64, f"bytes must stay bounded, saw {connection.queued_bytes}"
        assert connection.stats.events_dropped >= 1, "drops must be counted, not silent"
        assert connection.stats.events_received >= 8, "every event must be counted on arrival"

        # Bounded is not the same as empty: the events that fit must still be retrievable, with their
        # payloads intact, or a queue that drops everything would satisfy the assertions above.
        kept = []
        while True:
            event = connection.get_nowait()
            if event is None:
                break
            kept.append(event)
        assert len(kept) == 2, f"the two queued events must be retrievable, got {len(kept)}"
        assert all(e.type == "audio-chunk" and len(e.payload) == 16 for e in kept)
        assert len(kept) + connection.stats.events_dropped == connection.stats.events_received
    finally:
        await h.stop()


@check("9", "playback locks are per device, not per process")
async def t_per_device_locks() -> None:
    registry = SatelliteRegistry()
    one = await registry.slot_for(DeviceIdentity(device_id="kitchen-1"))
    two = await registry.slot_for(DeviceIdentity(device_id="study-1"))
    assert one.playback_lock is not two.playback_lock
    async with one.playback_lock:
        assert one.playback_lock.locked()
        # The other device's speaker is free: one room may talk while another is speaking.
        async with asyncio.timeout(1):
            async with two.playback_lock:
                pass


@check("10", "a device that never registers is dropped at the deadline")
async def t_registration_deadline() -> None:
    h = Harness()
    await h.start(registration_timeout=0.5)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", h.port)
        first = await asyncio.wait_for(async_read_event(reader), TIMEOUT)
        assert first is not None and first.type == "describe"
        # Say nothing at all: the listener must close the socket rather than hold it open.
        closed = False
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            try:
                event = await asyncio.wait_for(async_read_event(reader), 1.0)
            except asyncio.TimeoutError:
                continue
            if event is None:
                closed = True
                break
        writer.close()
        assert closed, "an unregistered connection must be closed rather than held open"
        assert len(h.registry) == 0
    finally:
        await h.stop()


@check("11", "an unsupported protocol version is refused clearly")
async def t_protocol_version() -> None:
    h = Harness()
    await h.start()
    try:
        dev = await h.device()
        reply = await dev.register("kitchen-1", protocol_version=PROTOCOL_VERSION + 1)
        assert reply is not None and reply.type == REGISTER_REFUSED_EVENT
        assert "protocol version" in reply.data.get("reason", "")
    finally:
        await h.stop()


@check("12", "the credential file fails closed: absent needs an opt-in, broken always refuses")
async def t_credential_file() -> None:
    import json
    import tempfile
    from bridge.registry import CredentialConfigError, load_credential_lookup

    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "device-credentials.json"

        # Absent, with no explicit opt-in: refuse. Absence is not consent to run unauthenticated (#62).
        try:
            load_credential_lookup(path)
            raise AssertionError("a missing credential file must not silently allow unauthenticated mode")
        except CredentialConfigError as exc:
            assert "no credential file" in str(exc)

        # Absent, with the explicit opt-in: trusted-network mode, and the caller gets None.
        assert load_credential_lookup(path, allow_unauthenticated=True) is None

        # Present but malformed: refuse, always. This is the regression that was filed as #62.
        path.write_text("{ this is not json")
        try:
            load_credential_lookup(path, allow_unauthenticated=True)
            raise AssertionError("a malformed credential file must refuse, even with the opt-in set")
        except CredentialConfigError as exc:
            assert "unusable" in str(exc)

        # Present but not an object, and present but empty: also refuse.
        path.write_text('["not", "a", "mapping"]')
        try:
            load_credential_lookup(path)
            raise AssertionError("a non-object credential file must refuse")
        except CredentialConfigError:
            pass
        path.write_text("{}")
        try:
            load_credential_lookup(path)
            raise AssertionError("an empty credential file must refuse rather than allow nobody in silence")
        except CredentialConfigError:
            pass

        # Valid: the mapping is used, and an unknown fingerprint resolves to None (refused by the caller).
        path.write_text(json.dumps({"sha256:abc": "kitchen-1"}))
        lookup = load_credential_lookup(path)
        assert lookup is not None
        assert lookup("sha256:abc") == "kitchen-1"
        assert lookup("sha256:something-else") is None


@check("13", "registration validation rejects what the contract does not allow")
async def t_validation_contract() -> None:
    h = Harness()
    await h.start()
    try:
        dev = await h.device()
        # A dictionary is not "a list of strings": it used to register as its keys.
        reply = await dev.register("kitchen-1", capabilities={"a": 1, "b": 2})
        assert reply is not None and reply.type == REGISTER_REFUSED_EVENT
        assert "list of strings" in reply.data.get("reason", "")

        dev2 = await h.device()
        reply = await dev2.register("kitchen-1", capabilities=["led", 7])
        assert reply is not None and reply.type == REGISTER_REFUSED_EVENT

        dev3 = await h.device()
        reply = await dev3.register("x" * 65)
        assert reply is not None and reply.type == REGISTER_REFUSED_EVENT
        assert "longer than" in reply.data.get("reason", "")

        dev4 = await h.device()
        reply = await dev4.register("kitchen 1; rm -rf /")
        assert reply is not None and reply.type == REGISTER_REFUSED_EVENT
        assert "only letters" in reply.data.get("reason", "")

        assert len(h.registry) == 0, "none of those should have created a slot"
    finally:
        await h.stop()


@check("15", "a credential minted after startup is recognised without a restart")
async def t_credential_reload() -> None:
    """The enrolment endpoint mints a certificate into device-credentials.json while the listener
    is already running. The registry must SEE it.

    It did not: load_credential_lookup froze its map at startup, while the enrol side re-reads the
    same file. So a satellite enrolled against a running bridge connected, presented the certificate
    the bridge had just issued, and was refused "no recognised credential" until someone restarted
    the service -- which breaks plug-and-play at exactly the moment a new satellite first appears."""
    import json
    import tempfile
    from bridge.registry import load_credential_lookup

    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "device-credentials.json"
        path.write_text(json.dumps({"sha256:aaaa": "device-one"}))

        lookup = load_credential_lookup(path)
        assert lookup("sha256:aaaa") == "device-one"
        assert lookup("sha256:bbbb") is None

        # The enrolment endpoint mints a second device into the SAME file while the listener runs.
        path.write_text(json.dumps({"sha256:aaaa": "device-one", "sha256:bbbb": "device-two"}))
        assert lookup("sha256:bbbb") == "device-two", (
            "a credential written after startup is invisible until the service restarts")

        # Revocation must take effect too: an entry removed from the file must stop being accepted.
        path.write_text(json.dumps({"sha256:aaaa": "device-one"}))
        assert lookup("sha256:bbbb") is None, (
            "a removed credential must be refused, not served from a stale cache")

        # A torn or unreadable file must not lock everybody out mid-write.
        path.write_text("{ this is not json")
        assert lookup("sha256:aaaa") == "device-one", (
            "an unreadable file must fall back to the last good map, not refuse everyone")


@check("14", "configuration reports are handled out of band and never leak into audio")
async def t_configuration_report_control_event() -> None:
    seen: list[tuple[str, dict]] = []

    async def on_control(device_id: str, event: Event) -> bool:
        seen.append((device_id, dict(event.data)))
        return event.type == CONFIG_REPORTED_EVENT

    h = Harness()
    await h.start(on_control_event=on_control)
    try:
        dev = await h.device()
        assert (await dev.register("kitchen-1")).type == REGISTER_OK_EVENT
        report = {
            "generation": 3,
            "actual": {"mic_gain": 78},
            "health": "healthy",
            "error": None,
        }
        await dev.send(CONFIG_REPORTED_EVENT, report)
        await asyncio.sleep(0.05)
        connection = h.registry.get("kitchen-1").connection
        assert seen == [("kitchen-1", report)]
        assert connection.get_nowait() is None, "control reports must not enter the audio queue"
    finally:
        await h.stop()


# ---------------------------------------------------------------- runner

@check("16", "a realistic audio burst does not overflow the DEFAULT ingress limits")
async def t_default_ingress_holds_real_audio() -> None:
    """The device sends 10 ms chunks; the bridge must not drop them while it is briefly busy.

    Regression, 2026-10-30: the default event cap was 64, which at 320 bytes per 10 ms chunk is
    0.64 s of audio. Any spell where the bridge was busy -- speech to text, a model call, streaming
    a reply -- overflowed it, and the log recorded the damage: "ingress full (64 events, 19840 bytes
    queued), dropped audio-chunk", 39 times in a few minutes. Dropped audio means eaten words.

    Five seconds of device audio is the bar: comfortably longer than any stage of a turn.
    """
    h = Harness()                      # DEFAULT limits, unlike the bounded-queue check above
    await h.start()
    try:
        dev = await h.device()
        assert (await dev.register("kitchen-1")).type == REGISTER_OK_EVENT
        connection = h.registry.get("kitchen-1").connection

        chunks = 500                   # 500 x 10 ms = 5 s of microphone audio
        for _ in range(chunks):
            await dev.send("audio-chunk", {}, b"\x01" * 320)
        await asyncio.sleep(0.3)

        assert connection.stats.events_dropped == 0, (
            f"5 s of audio must fit in the default ingress, dropped "
            f"{connection.stats.events_dropped} event(s) of {connection.stats.events_received}")
        assert connection.queued >= chunks, (
            f"all {chunks} chunks must still be queued, saw {connection.queued}")
    finally:
        await h.stop()


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
    print(f"\nregistry checks: {len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
