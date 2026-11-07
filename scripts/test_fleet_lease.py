#!/usr/bin/env python3
"""Every registered device must be leased, armed and wake-responsive (issue #111).

Run with the Gateway's own interpreter:

    ~/hermes-voice-satellite/.venv/bin/python scripts/test_fleet_lease.py

Why this exists. The Gateway leased exactly one satellite (`_first_ready_device` ->
`ready[0]`) and only re-leased after a lease loss. A second registered device sat
registered forever: its mic task ran, its wake scorer stayed disarmed because the
firmware only scores under `RUN_REQUESTED`, and it never answered "Okay Nabu".
Measured 2026-10-23: device generation 1..97, never leased, never armed.

The fleet rule this encodes: every registered connection gets its own lease, its
own supervisor reader, and a `run-satellite` arm. Wakes from any armed device queue
FIFO into one global turn. The winning device KEEPS ITS STREAM OPEN under the
activation word it heard (decided 2026-10-23); unfavourable devices get their listen
stream cancelled and are re-armed silently.

Offline shape, like scripts/test_registry.py: real sockets, real Wyoming framing,
real registry listener. Only the gateway, the models and `VoiceLoop.start()` are
left out; the seam under test is registration -> lease -> arm -> supervisor ->
wake routing, which runs without hardware.
"""

from __future__ import annotations

import asyncio
import pathlib
import socket
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wyoming.audio import AudioChunk, AudioStart  # noqa: E402
from wyoming.event import Event, async_read_event, async_write_event  # noqa: E402
from wyoming.wake import Detection  # noqa: E402

from satellite_gateway.loop import IdleWakeRefresh, VoiceLoop  # noqa: E402
from satellite_gateway.registry import (  # noqa: E402
    PROTOCOL_VERSION,
    REGISTER_EVENT,
    REGISTER_OK_EVENT,
    RegistryServer,
    SatelliteRegistry,
)

TIMEOUT = 3.0
checks = 0
failures: list[str] = []


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeDevice:
    """A fake satellite: a real socket, real framing, and a record of everything
    the Gateway ever sent it, in arrival order."""

    def __init__(self, port: int, device_id: str) -> None:
        self.port = port
        self.device_id = device_id
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.received: list[Event] = []
        self._reader_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        first = await asyncio.wait_for(async_read_event(self.reader), TIMEOUT)
        assert first is not None and first.type == "describe"
        await self.send("info", {"satellite": {"name": self.device_id, "area": "test-room"}})
        await self.send(REGISTER_EVENT, {
            "device_id": self.device_id,
            "name": self.device_id,
            "area": "test-room",
            "firmware_version": "test",
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": ["led", "speaker"],
            "boot_id": f"boot-{self.device_id}",
        })
        ok = await self.recv()
        assert ok is not None and ok.type == REGISTER_OK_EVENT, f"registration refused: {ok}"
        self._reader_task = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.reader is not None
        while True:
            event = await async_read_event(self.reader)
            if event is None:
                return
            self.received.append(event)

    def types_seen(self) -> list[str]:
        return [e.type for e in self.received]

    def count(self, event_type: str) -> int:
        return sum(1 for e in self.received if e.type == event_type)

    async def wait_for(self, event_type: str, timeout: float = TIMEOUT) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.count(event_type):
                return True
            await asyncio.sleep(0.05)
        return bool(self.count(event_type))

    async def send(self, event_or_type, data: dict | None = None) -> None:
        assert self.writer is not None
        event = event_or_type if isinstance(event_or_type, Event) else Event(
            type=event_or_type, data=data or {})
        await async_write_event(event, self.writer)
        await self.writer.drain()

    async def send_detection(self, name: str = "okay nabu", context: dict | None = None) -> None:
        await self.send(Detection(name=name, context=context).event())

    async def send_chunks(self, n: int) -> None:
        await self.send(AudioStart(rate=16000, width=2, channels=1).event())
        for _ in range(n):
            await self.send(AudioChunk(rate=16000, width=2, channels=1,
                                       audio=b"\x00\x01" * 320).event())

    async def recv(self, timeout: float = TIMEOUT) -> Event | None:
        assert self.reader is not None
        return await asyncio.wait_for(async_read_event(self.reader), timeout)

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass


class Harness:
    """A VoiceLoop wired to a real registry listener with the loop's own hooks."""

    def __init__(self) -> None:
        import tempfile
        from satellite_gateway.config_plane import ConfigPlane

        self._tmp = tempfile.TemporaryDirectory()
        self.loop = VoiceLoop(api_key="test")
        # Registration seeds a config record per device; keep those writes in a temp file,
        # never in the checkout's real state/fleet-config.json.
        self.loop.config_plane = ConfigPlane(pathlib.Path(self._tmp.name) / "fleet.json")
        # The turn machinery's collaborators: this test is about leases, routing and
        # arming, not whisper or the gateway.
        self.registry = SatelliteRegistry()
        self.loop.registry = self.registry
        self.port = free_port()
        self.server = RegistryServer(
            self.registry, "127.0.0.1", self.port,
            on_registered=self.loop._on_device_registered,
            on_control_event=self.loop._on_device_control_event,
        )
        self.devices: list[FakeDevice] = []

    async def start(self) -> None:
        await self.server.start()

    async def register(self, device_id: str) -> FakeDevice:
        device = FakeDevice(self.port, device_id)
        await device.start()
        self.devices.append(device)
        # Let the loop's registration hook finish (it may spawn a lease + arm).
        for _ in range(40):
            if device_id in getattr(self.loop, "leases", {}):
                break
            await asyncio.sleep(0.05)
        return device

    async def close(self) -> None:
        await self.loop.close()
        for device in self.devices:
            await device.close()
        await self.server.stop()
        self._tmp.cleanup()


async def scenario_fleet_arming() -> None:
    """Check 1-2: both registered devices get their own lease and a run-satellite arm."""
    h = Harness()
    await h.start()
    try:
        alpha = await h.register("fleet-alpha")
        beta = await h.register("fleet-beta")

        check("alpha leased", "fleet-alpha" in h.loop.leases, True)
        check("beta leased", "fleet-beta" in h.loop.leases, True)
        check("alpha armed with run-satellite", await alpha.wait_for("run-satellite"), True)
        check("beta armed with run-satellite", await beta.wait_for("run-satellite"), True)

        # Check 3: a wake on alpha claims the global turn; alpha's stream stays open.
        await alpha.send_detection()
        wake = await asyncio.wait_for(h.loop.wait_for_wake(timeout=3.0), TIMEOUT + 1)
        check("wake name routed", wake, "okay nabu")
        check("turn owner is alpha", h.loop.turn_owner, "fleet-alpha")

        # The winner keeps its stream open: no pause and no re-arm sent to alpha by
        # the claim itself (the arm from registration counts, nothing after it).
        check("alpha not paused by claim", alpha.count("pause-satellite"), 0)
        check("alpha not re-armed by claim", alpha.count("run-satellite"), 1)

        # Check 4: chunks the winner streamed after its detection are recoverable:
        # the supervisor buffered them and the turn drains the buffer first.
        await alpha.send_chunks(4)
        await asyncio.sleep(0.3)  # let the supervisor park on this lease's queue
        drained = []
        for _ in range(6):
            event = await h.loop._next(0.1)
            if event is None:
                break
            drained.append(event.type)
        check("winner's buffered audio reaches the turn",
              drained.count("audio-chunk") >= 3, True)

        # Check 5: beta wakes while alpha owns the turn -> queued FIFO, not dropped,
        # and beta's stream is left alone until arbitration decides otherwise.
        await beta.send_detection()
        await asyncio.sleep(0.3)
        check("beta's wake queued, not dropped", h.loop.wake_router.has_pending("fleet-beta"), True)

        # Release the turn (what the end of a turn does): the queued wake claims next.
        h.loop._release_turn_owner()
        name2 = await asyncio.wait_for(h.loop.wait_for_wake(timeout=3.0), TIMEOUT + 1)
        check("queued beta wake becomes the next turn", name2, "okay nabu")
        check("turn owner moved to beta", h.loop.turn_owner, "fleet-beta")
        check("beta stream kept open as winner", beta.count("pause-satellite"), 0)

        # Check 6: self-trigger suppression is per device, not global. Alpha just
        # answered, so alpha's echo is suppressed; beta's independent wake is not.
        h.loop._release_turn_owner()
        h.loop._speaking = True
        h.loop._speaking_at = __import__("time").monotonic()
        h.loop._speaking_for_device("fleet-beta", h.loop._speaking_at)
        await beta.send_detection()
        await asyncio.sleep(0.3)
        check("echo on the speaking device suppressed",
              h.loop.wake_router.has_pending("fleet-beta"), False)
        await alpha.send_detection()
        await asyncio.sleep(0.3)
        check("independent wake on a quiet device accepted",
              h.loop.wake_router.has_pending("fleet-alpha"), True)
    finally:
        await h.close()


async def scenario_loser_release() -> None:
    """Check 7-8: an unfavourable device gets its listen stream cancelled and a
    silent re-arm, and it answers again afterwards."""
    h = Harness()
    await h.start()
    try:
        alpha = await h.register("lose-alpha")
        beta = await h.register("lose-beta")
        check("both armed, alpha first",
              (await alpha.wait_for("run-satellite"), await beta.wait_for("run-satellite")),
              (True, True))

        # Alpha claims the turn; beta is unfavourable and is released.
        await alpha.send_detection()
        await h.loop.wait_for_wake(timeout=3.0)
        await beta.send_chunks(3)  # beta woke too and is streaming
        await asyncio.sleep(0.2)
        await h.loop._release_losing_stream("lose-beta", "out-heard by lose-alpha")

        types = beta.received.copy() if hasattr(beta, "received") else beta.types_seen()
        pause_at = -1
        for _ in range(40):
            types = beta.types_seen()
            pause_at = next((i for i, t in enumerate(types) if t == "pause-satellite"), -1)
            if pause_at >= 0:
                break
            await asyncio.sleep(0.05)
        runs = [i for i, t in enumerate(types) if t == "run-satellite"]
        # The re-arm follows the pause by one more loop step; bounded egress writes (#166)
        # put each send behind its own task, so poll for the pair rather than expecting
        # both in the snapshot that first caught the pause.
        for _ in range(20):
            if pause_at >= 0 and any(r > pause_at for r in runs):
                break
            await asyncio.sleep(0.05)
            types = beta.types_seen()
            runs = [i for i, t in enumerate(types) if t == "run-satellite"]
        check("beta's listen stream cancelled", pause_at >= 0, True)
        check("beta re-armed silently after the pause",
              any(r > pause_at for r in runs), True)
        check("no audio spoken on the released device",
              sum(1 for t in types if t in ("tts-audio", "audio-chunk")), 0)

        # Released devices stay wake-responsive: beta wakes again.
        h.loop._release_turn_owner()
        await beta.send_detection()
        name = await asyncio.wait_for(h.loop.wait_for_wake(timeout=3.0), TIMEOUT + 1)
        check("released device wakes again", name, "okay nabu")
    finally:
        await h.close()


async def scenario_liveness() -> None:
    """Check 9-10: a dead lease is dropped, not silently kept; idle raises the
    same IdleWakeRefresh the single-lease loop used to raise."""
    h = Harness()
    await h.start()
    try:
        await h.register("live-alpha")
        beta = await h.register("live-beta")
        await beta.close()
        for _ in range(40):
            if "fleet-beta" not in h.loop.leases and "live-beta" not in h.loop.leases:
                break
            await asyncio.sleep(0.05)
        dead_gone = not any(d.device_id in h.loop.leases for d in h.devices if d is beta)
        check("dead device lease dropped, not silent", dead_gone, True)

        raised = False
        try:
            await h.loop.wait_for_wake(timeout=0.5)
        except IdleWakeRefresh:
            raised = True
        check("idle with nobody speaking raises IdleWakeRefresh", raised, True)
    finally:
        await h.close()


async def scenario_announce_fanout() -> None:
    """Check 11-14 (Task A6): announcements across the fleet, the gate on the busy device
    only, and per-device playback locks that do not globally serialize."""
    from satellite_gateway.announce import Announcement
    from satellite_gateway.satellite import SatelliteClient

    h = Harness()
    await h.start()
    try:
        alpha = await h.register("an-alpha")
        beta = await h.register("an-beta")
        await alpha.wait_for("run-satellite")
        await beta.wait_for("run-satellite")

        announcer = h.loop.announcer
        targets = {getattr(s, "device_id", None) for s in announcer._delivery_targets()}
        check("idle announcement targets both armed devices", targets, {"an-alpha", "an-beta"})

        # A conversation active on alpha: the gate excludes ALPHA (its mic is open and its
        # speaker is busy, issue #19) but beta can still carry the announcement now.
        h.loop.turn_started_at = time.monotonic()
        h.loop.turn_owner = "an-alpha"
        targets = {getattr(s, "device_id", None) for s in announcer._delivery_targets()}
        check("during a turn on alpha, the announcement targets beta only", targets, {"an-beta"})
        waited = await announcer._wait_for_quiet(
            Announcement(id="an-test", text="test", source="test", key=None,
                         sound="ding", received_at=time.time()))
        check("beta-carried announcement does not wait out alpha's conversation",
              waited < 1.0, True)
        h.loop.turn_started_at = None
        h.loop.turn_owner = None

        # Single-satellite shape preserved: with no fleet leases, targets are [self._sat].
        announcer._satellites = lambda: []
        check("no leases falls back to the loop connection",
              [s is h.loop.sat for s in announcer._delivery_targets()], [True])
        announcer._satellites = lambda: list(h.loop.leases.values())

        # Per-device playback locks (issue #111): lease A's stream must not serialize
        # lease B's. Each lease carries its own lock; the class lock belongs to the dialled
        # path only.
        lease_a = h.loop.leases["an-alpha"]
        lease_b = h.loop.leases["an-beta"]
        check("leases carry their own playback locks",
              lease_a._playback_lock_override is not None
              and lease_b._playback_lock_override is not None
              and lease_a._playback_lock_override is not lease_b._playback_lock_override
              and lease_a._playback_lock_override is not SatelliteClient.PLAYBACK_LOCK,
              True)
        from satellite_gateway.tts import TtsStream

        async def sink(event):
            pass

        stream_a = lease_a.tts_stream(pace=0.0)
        async with stream_a:
            check("alpha's stream holds alpha's lock", lease_a.playback_lock.locked(), True)
            check("beta's lock stays free while alpha speaks", lease_b.playback_lock.locked(), False)
            # beta can open its own stream concurrently — no cross-device serialization.
            async with lease_b.tts_stream(pace=0.0):
                check("beta streams concurrently on its own lock", lease_b.playback_lock.locked(), True)
        check("alpha's lock released after its stream", lease_a.playback_lock.locked(), False)
    finally:
        await h.close()


async def scenario_level_arbitration() -> None:
    """Checks 15-19 (Milestone B, issue #32): with the claim window open, the device that
    heard the utterance BEST wins the turn, the loser's stream is cancelled and re-armed,
    and level-less detections degrade to first-arrival FIFO."""
    from satellite_gateway.wake_router import WakeRecord, pick_winner

    # -- pure unit: the ordering rule itself -------------------------------------
    def rec(device: str, level=None, score=None, at=0.0):
        detection = Detection(name="okay nabu",
                             context=({} if level is None and score is None
                                      else {k: v for k, v in
                                            (("wake_level", level), ("wake_score", score))
                                            if v is not None}))
        return WakeRecord(device_id=device, name="okay nabu",
                          arrived_at=at, detection=detection)

    pool = [rec("quiet", level=30, score=0.7, at=1.0), rec("loud", level=80, score=0.5, at=2.0)]
    check("loudest wins even when it arrived later", pick_winner(pool).device_id, "loud")
    pool = [rec("scorey", level=None, score=0.9, at=1.0), rec("measured", level=5, score=0.1, at=2.0)]
    check("any measured level outranks any score", pick_winner(pool).device_id, "measured")
    pool = [rec("first", at=1.0), rec("second", at=2.0)]
    check("no levels, no scores: FIFO wins", pick_winner(pool).device_id, "first")
    pool = [rec("a", level=None, at=1.0), rec("b", level=None, at=2.0)]
    check("garbage-free degradation is stable", pick_winner(pool).device_id, "a")
    check("garbage in context reads as no-level",
          WakeRecord(device_id="x", name="n", detection=type("D", (), {"context": {"wake_level": "loud"}})()).wake_level,
          None)

    # -- integration: real sockets, real detections, claim window open -----------
    h = Harness()
    await h.start()
    try:
        h.loop.wake_router.claim_window_s = 0.6
        near = await h.register("arb-near")
        far = await h.register("arb-far")
        await near.wait_for("run-satellite")
        await far.wait_for("run-satellite")

        # The person walks up to NEAR (louder); FAR hears the same utterance faintly 100ms later.
        await near.send_detection(context={"wake_level": 85, "wake_score": 0.9})
        await asyncio.sleep(0.1)
        await far.send_detection(context={"wake_level": 20, "wake_score": 0.95})

        name = await asyncio.wait_for(h.loop.wait_for_wake(timeout=3.0), TIMEOUT + 1)
        check("turn goes to the best-hearing device", h.loop.turn_owner, "arb-near")
        check("winner's wake name preserved", name, "okay nabu")
        # FAR was gathered within the window and released: pause then silent re-arm.
        types = far.types_seen()
        pause_at = -1
        for _ in range(40):
            types = far.types_seen()
            pause_at = next((i for i, t in enumerate(types) if t == "pause-satellite"), -1)
            if pause_at >= 0:
                break
            await asyncio.sleep(0.05)
        runs = [i for i, t in enumerate(types) if t == "run-satellite"]
        check("the loser's listen stream is cancelled", pause_at >= 0, True)
        check("the loser is re-armed silently after the pause", any(r > pause_at for r in runs), True)
        check("the winner keeps its stream open (no pause sent to near)", near.count("pause-satellite"), 0)

        # Reversed levels: FAR heard it better even though NEAR arrived first.
        h.loop._release_turn_owner()
        await asyncio.sleep(0.1)
        await near.send_detection(context={"wake_level": 15})
        await asyncio.sleep(0.1)
        await far.send_detection(context={"wake_level": 90})
        await asyncio.wait_for(h.loop.wait_for_wake(timeout=3.0), TIMEOUT + 1)
        check("arrival order never beats a better listener", h.loop.turn_owner, "arb-far")
        # The out-heard arrival (near) got cancelled — poll, the pause write lands async.
        pause_at = -1
        for _ in range(40):
            pause_at = next((i for i, t in enumerate(near.types_seen()) if t == "pause-satellite"), -1)
            if pause_at >= 0:
                break
            await asyncio.sleep(0.05)
        check("the out-heard arrival (near) got cancelled", pause_at >= 0, True)
        near_pauses = near.count("pause-satellite")

        # Both detections level-less (firmware rollback shape): first arrival still wins.
        h.loop._release_turn_owner()
        await asyncio.sleep(0.1)
        await near.send_detection()
        await asyncio.sleep(0.1)
        await far.send_detection()
        await asyncio.wait_for(h.loop.wait_for_wake(timeout=3.0), TIMEOUT + 1)
        check("level-less detections degrade to FIFO", h.loop.turn_owner, "arb-near")
        check("no extra cancels during a FIFO claim", near.count("pause-satellite"), near_pauses)
    finally:
        await h.close()


async def main() -> int:
    await scenario_fleet_arming()
    await scenario_loser_release()
    await scenario_liveness()
    await scenario_announce_fanout()
    await scenario_level_arbitration()
    print(f"{checks} checks, {len(failures)} failure(s)")
    for failure in failures:
        print(f"FAIL {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
