#!/usr/bin/env python3
"""Put "open the microphone" under the Gateway's control.

The choice, 2026-09-22, option 3. Follow-up mode needs the microphone opened with no wake word,
and a wyoming satellite in wake-word mode only streams audio after a wake detection:
`WakeStreamingSatellite` turns streaming off at the end of every utterance, re-arms wake detection and
logs "Waiting for wake word". Measured on the device: a follow-up window received 0 chunks of audio,
0.00 s, even with a real spoken sentence played out of the device's own speaker, and the same with
`--vad` (which this satellite ignores: "VAD is not used with local wake word detection").

So this small proxy sits between the satellite and wyoming-openwakeword. It forwards their traffic, and
on request writes a synthetic `Detection` into the satellite's connection. To the satellite that is
indistinguishable from the wake word being heard: it chimes, starts streaming and hands the audio to
the gateway. The Gateway therefore decides when the microphone opens, which is exactly what a follow-up
window needs.

Two additions, each owned here because the shim is the one process that sees both directions:

* **Real detections now carry the Pi-side wake level** (issue #32 arbitration). The shim measures
  the loudness of the microphone audio it is already relaying upstream — the exact same bytes the
  wake service scores — and attaches `context.wake_level` (0..100, the same fixed scale the ESP32
  wake core reports; see `docs/voice-detection.md`) when it relays a detection back down. The
  upstream audio itself is still forwarded byte for byte (check #1 stands); a stream the shim
  cannot parse simply stops producing measurements, never alters the forwarding. An injected
  wake-now detection has no wake audio behind it, so it stays the historical, context-free shape.
* The wake word itself is unaffected: detections arrive when the wake service says so, never here.

Topology, all loopback on the Pi:

    satellite  --tcp-->  wake_shim :10400  --tcp-->  wyoming-openwakeword :10401
                            |                         (its --uri moved to 10401)
                            +--> synthetic Detection, on request from :10402

The control port is loopback only, and in practice only the 2mic LED/event service talks to it, after
receiving a `wake-now` event that the Gateway sends over the satellite connection it already owns.
Nothing new is exposed to the LAN.

Run it with the satellite's virtualenv: it needs the `wyoming` package.

    ~/wyoming-satellite/.venv/bin/python3 wake_shim.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import signal
import sys
import time
from array import array
from collections import deque
from typing import Optional

_LOGGER = logging.getLogger("wake-shim")

DETECTION_TYPE = "detection"
LEVEL_WINDOW_SAMPLES = 1600          # ~100 ms at 16 kHz: brackets a spoken wake phrase
LEVEL_FRESH_SECONDS = 2.0            # a measurement older than this describes no live utterance
TAP_HEADER_MAX = 4096                # a Wyoming header longer than this is a desync
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 10400          # where the satellite connects (its --wake-uri)
DEFAULT_UPSTREAM_HOST = "127.0.0.1"
DEFAULT_UPSTREAM_PORT = 10401        # where the real wake service now listens
DEFAULT_CONTROL_HOST = "127.0.0.1"
DEFAULT_CONTROL_PORT = 10402         # loopback-only control
DEFAULT_WAKE_NAME = "ok_nabu"
READ_SIZE = 65536


def _positive_length(value) -> int:
    """A Wyoming declared length, or 0. bools are ints in Python; a `true` in a
    length field is not a one-byte body, it is a confused writer."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


class _WakeLevelTap:
    """Passive loudness meter on the satellite -> wake service stream (issue #32).

    The shim already relays, byte for byte, the microphone audio that wyoming-openwakeword
    scores. This tap is fed the same bytes and *measures only*: it parses Wyoming framing to
    find audio-chunk payloads and keeps a fixed window of their amplitude (the last
    LEVEL_WINDOW_SAMPLES samples), so a detection relayed back down can report what the room
    sounded like at this device. The scale and rules mirror the ESP32 wake core exactly
    (docs/voice-detection.md): 20*log10(rms/32768) mapped over [-60 dBFS, 0 dBFS] onto
    0..100; exact silence is 0 (the floor, a measurement); nothing measured is None.

    Framing safety is the contract that makes this safe to put on the audio path at all: the
    tap can never alter, hold or reorder the bytes it is fed -- `_relay_bytes` forwards them
    independently, because the wake word depends on the relay being byte-for-byte. If the tap
    cannot follow the framing (unparseable header, a header over TAP_HEADER_MAX), it turns
    itself off with one log line and stays off until the next satellite connection. A meter
    that stops is a lost metric; a relay that breaks is a deaf house.
    """

    PHASE_HEADER = 0
    PHASE_BODY = 1
    PHASE_PAYLOAD = 2

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._buffer = bytearray()
        self._phase = self.PHASE_HEADER
        self._want = 0
        self._payload_want = 0
        self._unit = 2                # bytes per sample for the payload being consumed
        self._measure_payload = False
        self._squares: "deque[float]" = deque(maxlen=LEVEL_WINDOW_SAMPLES)
        self.last_sample_at: Optional[float] = None
        self._desynced = False

    def begin_connection(self) -> None:
        """A new upstream connection is a new audio stream; the window describes the new room."""
        self.reset()

    def feed(self, data: bytes) -> None:
        """Take the same bytes the relay forwards. Never returns them, never touches the relay."""
        if self._desynced:
            return
        self._buffer.extend(data)
        while not self._desynced:
            if self._phase == self.PHASE_HEADER:
                newline = self._buffer.find(b"\n")
                if newline < 0:
                    if len(self._buffer) > TAP_HEADER_MAX:
                        self._desync()
                    return  # header still incomplete: wait, the relay is not waiting on me
                line = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                try:
                    header = json.loads(line)
                except ValueError:
                    self._desync()
                    return
                if not isinstance(header, dict):
                    self._desync()
                    return
                self._measure_payload = header.get("type") == "audio-chunk"
                self._unit = 2
                self._payload_want = _positive_length(header.get("payload_length"))
                data_length = _positive_length(header.get("data_length"))
                if data_length > 0:
                    self._want = data_length
                    self._phase = self.PHASE_BODY
                elif self._payload_want > 0:
                    self._phase = self.PHASE_PAYLOAD
            elif self._phase == self.PHASE_BODY:
                if len(self._buffer) < self._want:
                    return
                body = bytes(self._buffer[:self._want])
                del self._buffer[:self._want]
                if self._measure_payload:
                    chunk_data: Optional[dict] = None
                    try:
                        parsed = json.loads(body)
                        if isinstance(parsed, dict):
                            chunk_data = parsed
                    except ValueError:
                        chunk_data = None
                    width = chunk_data.get("width") if chunk_data is not None else None
                    channels = chunk_data.get("channels") if chunk_data is not None else None
                    if width == 2:
                        self._unit = 2 * (channels
                                          if isinstance(channels, int) and not isinstance(channels, bool)
                                          and channels > 0 else 1)
                    else:
                        # Not 16-bit samples: amplitude on some other scale is not comparable
                        # with the fixed contract. Skip the audio, keep following the framing.
                        self._measure_payload = False
                self._phase = self.PHASE_PAYLOAD if self._payload_want > 0 else self.PHASE_HEADER
            elif self._phase == self.PHASE_PAYLOAD:
                if len(self._buffer) < self._payload_want:
                    return
                payload = bytes(self._buffer[:self._payload_want])
                del self._buffer[:self._payload_want]
                self._phase = self.PHASE_HEADER
                if self._measure_payload:
                    self._measure(payload)

    def _measure(self, payload: bytes) -> None:
        samples = len(payload) // self._unit
        if samples <= 0:
            return
        frame = array("h")
        frame.frombytes(payload[:samples * self._unit])
        if sys.byteorder != "little":
            frame.byteswap()  # Wyoming carries little-endian s16
        self._squares.extend((sample / 32768.0) ** 2 for sample in frame)
        self.last_sample_at = time.monotonic()

    def level(self) -> Optional[int]:
        """The fixed-scale level of the current window, or None when nothing was measured."""
        if not self._squares:
            return None
        mean_square = sum(self._squares) / len(self._squares)
        if mean_square <= 0.0:
            return 0  # exact silence is the floor, and the floor is a measurement
        db = 10.0 * math.log10(mean_square)  # 20*log10(rms) == 10*log10(mean_square)
        if db <= -60.0:
            return 0
        if db >= 0.0:
            return 100
        return int((db + 60.0) / 60.0 * 100.0 + 0.5)

    def _desync(self) -> None:
        if not self._desynced:
            _LOGGER.info("wake level tap lost framing; measuring stops until the next satellite "
                         "connection (the byte-for-byte audio relay itself is unaffected)")
        self._desynced = True
        self._buffer = bytearray()


class WakeShim:
    """Relays satellite <-> wake service, and can inject a wake detection on request."""

    def __init__(self, *, listen_host: str, listen_port: int,
                 upstream_host: str, upstream_port: int,
                 control_host: str, control_port: int,
                 wake_name: str) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.control_host = control_host
        self.control_port = control_port
        self.wake_name = wake_name
        self._sat_writer: Optional[asyncio.StreamWriter] = None
        # Every writer to the satellite shares this lock. A Wyoming event is a JSON
        # header followed by declared data/payload bytes; another event may not land
        # between those pieces (#17).
        self._sat_write_lock = asyncio.Lock()
        self._level_tap = _WakeLevelTap()   # passenger on the satellite -> wake service relay
        self.injections = 0

    # -- relay ---------------------------------------------------------------

    async def _relay_bytes(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        """Satellite -> wake service, straight through. Nothing else writes that direction.

        Byte for byte is non-negotiable (wake detection depends on it), so the level tap is a
        passenger: it is fed the same bytes and never holds, buffers, re-frames or reorders any
        of them. If the tap ever loses track of the framing it stops measuring and says so once;
        the audio keeps flowing unchanged.
        """
        tap = self._level_tap
        tap.begin_connection()
        try:
            while True:
                data = await reader.read(READ_SIZE)
                if not data:
                    break
                tap.feed(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            _LOGGER.info("satellite -> wake service relay ended: %s", exc or "EOF")
        else:
            _LOGGER.info("satellite -> wake service relay ended: EOF")

    def _level_for_detection(self) -> Optional[int]:
        """The level to attach to a detection coming down from the wake service.

        The measurement describes the audio the wake service just scored, because it is the same
        bytes (the tap above). Two things can legitimately mean "no level", and both relay the
        historical, context-free detection: nothing measured yet on this connection (a shim that
        started mid-stream, or audio the tap could not parse), and a measurement too old to
        describe this utterance.
        """
        sample_time = self._level_tap.last_sample_at
        if sample_time is None:
            return None
        if time.monotonic() - sample_time > LEVEL_FRESH_SECONDS:
            return None
        return self._level_tap.level()

    async def _relay_events(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        """Wake service -> satellite, one whole event per write.

        Deliberately not a byte copy: an injected detection must never land in the middle of a
        relayed event, or the satellite would see a corrupted stream. The upstream only ever sends
        detections and its info block, so this is cheap.

        A detection additionally gets `context.wake_level` (issue #32): the header is rewritten
        and its declared `data_length` recomputed, header and body still written as one event
        under one lock hold. A detection with no fresh measurement, or one whose body is not the
        JSON the shim expects, is relayed exactly as received -- the historical shape, unchanged.
        """
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    header = json.loads(line)
                except ValueError:
                    _LOGGER.warning("upstream sent a line that is not JSON; dropping it")
                    continue

                # The lock spans the whole event, reads included (the upstream has already sent
                # what it declared, so a read under the lock completes promptly). An injected
                # detection must never slip between a header and its declared body (#17).
                async with self._sat_write_lock:
                    body = b""
                    rewritten = None
                    data_length = _positive_length(header.get("data_length"))
                    if data_length > 0:
                        body = await reader.readexactly(data_length)
                    if header.get("type") == DETECTION_TYPE:
                        # Both Wyoming shapes carry detection data: inline in the header, or as a
                        # declared JSON body. A fresh measurement goes into whichever one is used.
                        level = self._level_for_detection()
                        if level is not None:
                            rewritten = self._attach_level(header, body, level)

                    payload_length = header.get("payload_length")
                    payload = (await reader.readexactly(payload_length)
                               if isinstance(payload_length, int) and payload_length > 0 else b"")

                    if rewritten is not None:
                        writer.write(rewritten)
                    else:
                        writer.write(line)
                        if body:
                            writer.write(body)
                    if payload:
                        writer.write(payload)
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError, asyncio.IncompleteReadError) as exc:
            _LOGGER.info("wake service -> satellite relay ended: %s", exc or "EOF")
        else:
            _LOGGER.info("wake service -> satellite relay ended: EOF")

    @staticmethod
    def _attach_level(header: dict, body: bytes, level: int) -> Optional[bytes]:
        """Header (+ body, when the event has one) rewritten with `context.wake_level`.

        Wyoming data travels either inline in the header or as a declared JSON body; both shapes
        are handled. If the body is not the JSON object the shim expects, returns None and the
        caller relays the event untouched instead of guessing.
        """
        if body:
            try:
                parsed = json.loads(body)
            except ValueError:
                return None
            if not isinstance(parsed, dict):
                return None
            target = parsed
        else:
            inline = header.get("data")
            if not isinstance(inline, dict):
                return None  # a detection whose data is a bare string has nowhere to put context
            target = inline
        context = target.get("context")
        if context is None:
            context = {}
        elif not isinstance(context, dict):
            return None
        context.setdefault("wake_level", level)
        target["context"] = context
        new_header = dict(header)
        if body:
            new_body = json.dumps(parsed).encode()
            new_header["data_length"] = len(new_body)
            return json.dumps(new_header).encode() + b"\n" + new_body
        return json.dumps(new_header).encode() + b"\n"

    async def handle_satellite(self, sat_reader: asyncio.StreamReader,
                               sat_writer: asyncio.StreamWriter) -> None:
        """One satellite connection, with an upstream connection that is retried if it drops."""
        peer = sat_writer.get_extra_info("peername")
        _LOGGER.info("satellite connected from %s", peer)
        self._sat_writer = sat_writer
        try:
            while True:
                try:
                    up_reader, up_writer = await asyncio.open_connection(
                        self.upstream_host, self.upstream_port)
                except OSError as exc:
                    _LOGGER.warning("wake service at %s:%d not reachable (%s); retrying",
                                    self.upstream_host, self.upstream_port, exc)
                    await asyncio.sleep(2.0)
                    if sat_writer.is_closing():
                        return
                    continue

                _LOGGER.info("relaying to the wake service at %s:%d",
                             self.upstream_host, self.upstream_port)
                to_up = asyncio.create_task(self._relay_bytes(sat_reader, up_writer),
                                            name="sat->wake")
                to_sat = asyncio.create_task(self._relay_events(up_reader, sat_writer),
                                             name="wake->sat")
                done, pending = await asyncio.wait({to_up, to_sat},
                                                   return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in pending:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                up_writer.close()
                try:
                    await up_writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass

                if sat_writer.is_closing():
                    return
                _LOGGER.warning("wake service connection ended; reconnecting")
                await asyncio.sleep(0.5)
        finally:
            if self._sat_writer is sat_writer:
                self._sat_writer = None
            _LOGGER.info("satellite disconnected")

    # -- control -------------------------------------------------------------

    async def inject(self) -> bool:
        """Write a synthetic Detection to the satellite: the same event a real wake produces."""
        writer = self._sat_writer
        if writer is None or writer.is_closing():
            _LOGGER.warning("asked to open the microphone but no satellite is connected")
            return False
        async with self._sat_write_lock:
            # The writer may have disconnected while this injection waited behind a
            # complete relayed event. Never write into a superseded connection.
            if writer is not self._sat_writer or writer.is_closing():
                _LOGGER.warning("asked to open the microphone but the satellite disconnected")
                return False
            writer.write((json.dumps({"type": DETECTION_TYPE,
                                      "data": {"name": self.wake_name}}) + "\n").encode())
            await writer.drain()
        self.injections += 1
        _LOGGER.info("injected a wake detection (%s): the microphone is open", self.wake_name)
        return True

    async def handle_control(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), timeout=5.0)).decode(
                errors="replace").strip().lower()
        except (asyncio.TimeoutError, OSError):
            line = ""
        if line in ("wake", "wake-now", "listen"):
            ok = await self.inject()
            writer.write(b"ok\n" if ok else b"no-satellite\n")
        else:
            writer.write(b"usage: wake\n")
        try:
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        writer.close()

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        control = await asyncio.start_server(self.handle_control,
                                             self.control_host, self.control_port)
        upstream = await asyncio.start_server(self.handle_satellite,
                                              self.listen_host, self.listen_port)
        _LOGGER.info("wake shim: satellite on %s:%d -> wake service %s:%d, control on %s:%d",
                     self.listen_host, self.listen_port,
                     self.upstream_host, self.upstream_port,
                     self.control_host, self.control_port)
        async with upstream, control:
            await asyncio.gather(upstream.serve_forever(), control.serve_forever())


async def main() -> int:
    parser = argparse.ArgumentParser(description="wake-word shim with on-demand microphone release")
    parser.add_argument("--listen-host", default=DEFAULT_LISTEN_HOST)
    parser.add_argument("--listen-port", type=int, default=DEFAULT_LISTEN_PORT)
    parser.add_argument("--upstream-host", default=DEFAULT_UPSTREAM_HOST)
    parser.add_argument("--upstream-port", type=int, default=DEFAULT_UPSTREAM_PORT)
    parser.add_argument("--control-host", default=DEFAULT_CONTROL_HOST)
    parser.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT)
    parser.add_argument("--wake-name", default=DEFAULT_WAKE_NAME)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    shim = WakeShim(listen_host=args.listen_host, listen_port=args.listen_port,
                    upstream_host=args.upstream_host, upstream_port=args.upstream_port,
                    control_host=args.control_host, control_port=args.control_port,
                    wake_name=args.wake_name)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: [t.cancel() for t in asyncio.all_tasks()])
        except NotImplementedError:  # non-POSIX fallback
            pass

    await shim.run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (asyncio.CancelledError, KeyboardInterrupt):
        sys.exit(0)
