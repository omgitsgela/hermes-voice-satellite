#!/usr/bin/env python3
"""Prove the wake shim relays faithfully and can open the microphone on request.

No device, no wake word, no network beyond loopback: a fake wake service and a fake satellite stand in
for the real ones.

What matters, in order of how badly it would hurt if it broke:

  * the satellite's audio reaches the wake service byte for byte (wake detection depends on it);
  * an event from the wake service reaches the satellite intact, payload included;
  * a requested injection arrives as a valid `detection` event;
  * an injection never corrupts a relayed event (this is why the upstream direction is
    event-at-a-time rather than a byte copy);
  * a real detection relayed back down carries `context.wake_level`, measured from the same
    audio the shim relayed upstream (issue #32), on the fixed 0..100 scale;
  * with no satellite connected, the control port says so instead of pretending;

    ~/hermes-voice-satellite/.venv/bin/python test_wake_shim.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wake_shim import WakeShim  # noqa: E402

PROBLEMS = 0


def check(label: str, ok: bool, detail: "object" = "") -> None:
    global PROBLEMS
    print(f"   {'OK  ' if ok else 'FAIL'} {label}{(' -- ' + str(detail)) if detail else ''}")
    if not ok:
        PROBLEMS += 1


async def start(handler, host="127.0.0.1"):
    server = await asyncio.start_server(handler, host, 0)
    return server, server.sockets[0].getsockname()[1]


class FakeWakeService:
    """Stands in for wyoming-openwakeword: reports what it received, can send events."""

    def __init__(self):
        self.received = bytearray()
        self.sat_writers = []
        self.server = None
        self.port = 0

    async def handle(self, reader, writer):
        self.sat_writers.append(writer)
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                self.received.extend(data)
        except (ConnectionResetError, OSError):
            pass

    async def start(self):
        self.server, self.port = await start(self.handle)

    async def send_event(self, event: dict, payload: bytes = b"") -> None:
        writer = self.sat_writers[-1]
        line = json.dumps(event).encode() + b"\n"
        if payload:
            writer.write(line + payload)
        else:
            writer.write(line)
        await writer.drain()


class FakeSatellite:
    """Stands in for the satellite: sends bytes, collects everything it receives."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.events = []

    @classmethod
    async def connect(cls, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        return cls(reader, writer)

    async def collect(self, seconds: float) -> None:
        """Read events the way the satellite does: a JSON line, then any declared payload.

        Honouring data_length/payload_length is what keeps event boundaries straight: without it the
        payload bytes are read as if they were the next line, and a later event looks glued to them.
        """
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            try:
                line = await asyncio.wait_for(self.reader.readline(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            if not line:
                return
            try:
                event = json.loads(line)
            except ValueError:
                self.events.append({"raw": line.decode(errors="replace")})
                continue
            for key in ("data_length", "payload_length"):
                length = event.get(key)
                if isinstance(length, int) and length > 0:
                    body = await self.reader.readexactly(length)
                    event["_payload_bytes"] = event.get("_payload_bytes", 0) + len(body)
            self.events.append(event)


async def send_control(port: int, command: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write((command + "\n").encode())
    await writer.drain()
    reply = (await asyncio.wait_for(reader.readline(), timeout=5.0)).decode().strip()
    writer.close()
    return reply


class FragmentedEventReader:
    """Release a Wyoming header now and its declared body only when the test says so."""

    def __init__(self, header: bytes, body: bytes) -> None:
        self.header = header
        self.body = body
        self.header_seen = asyncio.Event()
        self.release_body = asyncio.Event()
        self.sent = False

    async def readline(self) -> bytes:
        if self.sent:
            return b""
        self.sent = True
        self.header_seen.set()
        return self.header

    async def readexactly(self, _length: int) -> bytes:
        await self.release_body.wait()
        return self.body


class CollectingWriter:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return False


async def main() -> int:
    print("=== WAKE SHIM (loopback only) ===")

    wake = FakeWakeService()
    await wake.start()

    shim = WakeShim(listen_host="127.0.0.1", listen_port=0,
                    upstream_host="127.0.0.1", upstream_port=wake.port,
                    control_host="127.0.0.1", control_port=0, wake_name="ok_nabu")
    listen_server, listen_port = await start(shim.handle_satellite)
    control_server, control_port = await start(shim.handle_control)

    # 1. The satellite's audio must reach the wake service byte for byte.
    sat = await FakeSatellite.connect(listen_port)
    audio = json.dumps({"type": "audio-chunk", "data": {"rate": 16000},
                        "payload_length": 320}).encode() + b"\n" + b"\x01\x02" * 160
    sat.writer.write(audio)
    await sat.writer.drain()
    for _ in range(40):
        if len(wake.received) >= len(audio):
            break
        await asyncio.sleep(0.05)
    check("the satellite's audio reaches the wake service unchanged",
          bytes(wake.received) == audio, f"{len(wake.received)}/{len(audio)} bytes")

    # 2. A wake service event must reach the satellite intact, payload included.
    payload = b"\xaa\xbb" * 20
    await wake.send_event({"type": "info", "data": {"note": "hello"}, "payload_length": len(payload)},
                          payload)
    await asyncio.sleep(0.2)
    await sat.collect(0.6)
    check("an event from the wake service arrives as one valid event",
          any(e.get("type") == "info" and e.get("data", {}).get("note") == "hello"
              for e in sat.events), sat.events[:3])

    # 3. On request, a valid detection must arrive.
    before = len(sat.events)
    reply = await send_control(control_port, "wake")
    await sat.collect(0.6)
    new = [e for e in sat.events[before:] if e.get("type") == "detection"]
    check("the control port reports success", reply == "ok", repr(reply))
    check("a synthetic detection arrives, named for the wake word",
          len(new) == 1 and new[0].get("data", {}).get("name") == "ok_nabu", new)
    check("the shim counted the injection", shim.injections == 1, shim.injections)

    # 4. An injection must not corrupt a relayed event. Send a payload event and inject at once.
    before = len(sat.events)
    payload2 = b"\x11\x22" * 64
    await asyncio.gather(
        wake.send_event({"type": "info", "data": {"note": "concurrent"},
                         "payload_length": len(payload2)}, payload2),
        send_control(control_port, "wake"),
    )
    await asyncio.sleep(0.2)
    await sat.collect(0.8)
    fresh = sat.events[before:]
    intact = any(e.get("type") == "info" and e.get("data", {}).get("note") == "concurrent"
                 for e in fresh)
    detections = sum(1 for e in fresh if e.get("type") == "detection")
    check("an injection alongside a relayed event: the relayed event survives", intact, fresh[:4])
    check("an injection alongside a relayed event: the detection still arrives",
          detections >= 1, f"detections={detections}")

    # 4b. The hard framing case: the upstream header arrives before its declared body. An injection
    # in that gap must not split the event by landing between the header and body (#17).
    body = b"{}"
    header = json.dumps({"type": "info", "data_length": len(body)}).encode() + b"\n"
    reader = FragmentedEventReader(header, body)
    writer = CollectingWriter()
    framing_shim = WakeShim(
        listen_host="127.0.0.1", listen_port=0,
        upstream_host="127.0.0.1", upstream_port=0,
        control_host="127.0.0.1", control_port=0, wake_name="ok_nabu",
    )
    framing_shim._sat_writer = writer  # type: ignore[assignment]
    relay = asyncio.create_task(
        framing_shim._relay_events(reader, writer)  # type: ignore[arg-type]
    )
    await reader.header_seen.wait()
    await asyncio.sleep(0)
    inject = asyncio.create_task(framing_shim.inject())
    await asyncio.sleep(0)
    reader.release_body.set()
    await asyncio.gather(relay, inject)
    detection = (json.dumps({"type": "detection", "data": {"name": "ok_nabu"}}) + "\n").encode()
    check("a fragmented relayed event remains atomic across an injection",
          bytes(writer.data) == header + body + detection, repr(bytes(writer.data)))

    # 5. Wake level for arbitration (issue #32): the shim measures the audio it relays upstream
    # and puts the measurement into the detection it relays back down.
    from wake_shim import _WakeLevelTap
    import struct

    def audio_chunk(amplitude: int, samples: int = 160) -> bytes:
        payload = b"".join(struct.pack("<h", amplitude) for _ in range(samples))
        return (json.dumps({"type": "audio-chunk",
                            "data": {"rate": 16000, "width": 2, "channels": 1},
                            "payload_length": len(payload)}).encode() + b"\n" + payload)

    tap = _WakeLevelTap()
    tap.feed(audio_chunk(1024))
    check("the level scale matches the fixed contract (1024/32768 full scale is 50)",
          tap.level() == 50, tap.level())
    # The window is the last LEVEL_WINDOW_SAMPLES samples (~100 ms): it brackets a spoken wake
    # phrase, so a few chunks blend, and sustained newer audio pushes older audio out.
    for _ in range(11):
        tap.feed(audio_chunk(16384))   # 11 * 160 > 1600: the quiet chunk is fully out of the window
    check("the window follows the newest audio (a louder phrase pushes the quiet one out)",
          tap.level() == 90, tap.level())
    # Silence needs its own window: 160 silent samples inside a window still holding 1440 loud
    # ones is a blend, not a measurement of the silence.
    silent = _WakeLevelTap()
    silent.feed(audio_chunk(0))
    check("measured silence is 0 (the floor), not None", silent.level() == 0, silent.level())
    check("no audio measured is None", _WakeLevelTap().level() is None)
    quiet = _WakeLevelTap()
    quiet.feed(b"this is not a wyoming header\n")
    check("a tap that loses framing reports no level", quiet.level() is None)
    quiet.feed(audio_chunk(16384))
    check("a desynced tap stays off (it never re-guesses the framing)", quiet.level() is None)

    # End to end through the live shim: satellite audio up, detection down with the level.
    # The window is opened fresh here so the measurement describes only this scenario's audio.
    shim._level_tap.reset()
    before = len(sat.events)
    sat.writer.write(audio_chunk(1024))
    await sat.writer.drain()
    for _ in range(40):
        if shim._level_tap.level() is not None:
            break
        await asyncio.sleep(0.05)
    measured = shim._level_tap.level()
    await wake.send_event({"type": "detection", "data": {"name": "ok_nabu", "score": 0.9}})
    await asyncio.sleep(0.2)
    await sat.collect(0.6)
    fresh = [e for e in sat.events[before:] if e.get("type") == "detection"]
    carried = fresh[0].get("data", {}).get("context", {}).get("wake_level") if fresh else None
    check("a real detection relayed down carries the measured wake_level",
          len(fresh) == 1 and measured == 50 and carried == measured,
          f"measured={measured} carried={carried} events={fresh}")
    check("the rewrite preserves the rest of the detection (name, score)",
          len(fresh) == 1 and fresh[0].get("data", {}).get("name") == "ok_nabu"
          and fresh[0].get("data", {}).get("score") == 0.9, fresh)

    # An existing context is merged into, never replaced.
    before = len(sat.events)
    await wake.send_event({"type": "detection",
                           "data": {"name": "ok_nabu", "context": {"source": "wakeword"}}})
    await asyncio.sleep(0.2)
    await sat.collect(0.6)
    fresh = [e for e in sat.events[before:] if e.get("type") == "detection"]
    check("a detection that already has a context keeps its own keys and gains wake_level",
          len(fresh) == 1
          and fresh[0].get("data", {}).get("context", {}).get("source") == "wakeword"
          and fresh[0].get("data", {}).get("context", {}).get("wake_level") == 50,
          fresh)

    # A stale measurement describes no live utterance: the detection keeps the historical shape.
    assert shim._level_tap.last_sample_at is not None  # the check above just measured it
    shim._level_tap.last_sample_at -= 10.0
    before = len(sat.events)
    await wake.send_event({"type": "detection", "data": {"name": "ok_nabu"}})
    await asyncio.sleep(0.2)
    await sat.collect(0.6)
    fresh = [e for e in sat.events[before:] if e.get("type") == "detection"]
    check("a stale measurement relays the historical, context-free detection",
          len(fresh) == 1 and "context" not in fresh[0].get("data", {}), fresh)

    # An injected wake-now detection has no wake audio behind it: it stays context-free even
    # while a fresh measurement exists (it would be a lie to claim a level for audio nobody spoke).
    before = len(sat.events)
    reply = await send_control(control_port, "wake")
    await sat.collect(0.6)
    fresh = [e for e in sat.events[before:] if e.get("type") == "detection"]
    check("a synthetic injection stays the historical, context-free shape",
          reply == "ok" and len(fresh) == 1 and "context" not in fresh[0].get("data", {}),
          fresh)

    # 6. A rejected command must not open the microphone.
    reply = await send_control(control_port, "please listen")
    check("an unknown control command is refused", reply.startswith("usage"), repr(reply))

    sat.writer.close()
    await asyncio.sleep(0.2)

    # 6. With no satellite connected, say so rather than pretending.
    shim._sat_writer = None
    reply = await send_control(control_port, "wake")
    check("with no satellite it reports no-satellite", reply == "no-satellite", repr(reply))

    # 7. Everything is bound to loopback.
    from wake_shim import (DEFAULT_CONTROL_HOST, DEFAULT_LISTEN_HOST, DEFAULT_UPSTREAM_HOST)
    check("default hosts are all 127.0.0.1",
          {DEFAULT_LISTEN_HOST, DEFAULT_CONTROL_HOST, DEFAULT_UPSTREAM_HOST} == {"127.0.0.1"},
          (DEFAULT_LISTEN_HOST, DEFAULT_CONTROL_HOST, DEFAULT_UPSTREAM_HOST))

    listen_server.close()
    control_server.close()
    wake.server.close()
    print(f"\n=== {'ALL CHECKS PASSED' if PROBLEMS == 0 else f'{PROBLEMS} PROBLEM(S)'} ===")
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
