#!/usr/bin/env python3
"""Put "open the microphone" under the bridge's control.

Angela's choice, 2026-09-22, option 3. Follow-up mode needs the microphone opened with no wake word,
and a wyoming satellite in wake-word mode only streams audio after a wake detection:
`WakeStreamingSatellite` turns streaming off at the end of every utterance, re-arms wake detection and
logs "Waiting for wake word". Measured on the device: a follow-up window received 0 chunks of audio,
0.00 s, even with a real spoken sentence played out of the device's own speaker, and the same with
`--vad` (which this satellite ignores: "VAD is not used with local wake word detection").

So this small proxy sits between the satellite and wyoming-openwakeword. It forwards their traffic, and
on request writes a synthetic `Detection` into the satellite's connection. To the satellite that is
indistinguishable from the wake word being heard: it chimes, starts streaming and hands the audio to
the bridge. The bridge therefore decides when the microphone opens, which is exactly what a follow-up
window needs. The wake word itself is unaffected: real detections are relayed untouched.

Topology, all loopback on the Pi:

    satellite  --tcp-->  wake_shim :10400  --tcp-->  wyoming-openwakeword :10401
                            |                         (its --uri moved to 10401)
                            +--> synthetic Detection, on request from :10402

The control port is loopback only, and in practice only the 2mic LED/event service talks to it, after
receiving a `wake-now` event that the bridge sends over the satellite connection it already owns.
Nothing new is exposed to the LAN.

Run it with the satellite's virtualenv: it needs the `wyoming` package.

    /home/wyoming1/wyoming-satellite/.venv/bin/python3 wake_shim.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from typing import Optional

_LOGGER = logging.getLogger("wake-shim")

DETECTION_TYPE = "detection"
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 10400          # where the satellite connects (its --wake-uri)
DEFAULT_UPSTREAM_HOST = "127.0.0.1"
DEFAULT_UPSTREAM_PORT = 10401        # where the real wake service now listens
DEFAULT_CONTROL_HOST = "127.0.0.1"
DEFAULT_CONTROL_PORT = 10402         # loopback-only control
DEFAULT_WAKE_NAME = "ok_nabu"
READ_SIZE = 65536


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
        self.injections = 0

    # -- relay ---------------------------------------------------------------

    async def _relay_bytes(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        """Satellite -> wake service, straight through. Nothing else writes that direction."""
        try:
            while True:
                data = await reader.read(READ_SIZE)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            _LOGGER.info("satellite -> wake service relay ended: %s", exc or "EOF")
        else:
            _LOGGER.info("satellite -> wake service relay ended: EOF")

    async def _relay_events(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        """Wake service -> satellite, one whole event per write.

        Deliberately not a byte copy: an injected detection must never land in the middle of a
        relayed event, or the satellite would see a corrupted stream. The upstream only ever sends
        detections and its info block, so this is cheap.
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
                writer.write(line)
                for key in ("data_length", "payload_length"):
                    length = header.get(key)
                    if isinstance(length, int) and length > 0:
                        writer.write(await reader.readexactly(length))
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError, asyncio.IncompleteReadError) as exc:
            _LOGGER.info("wake service -> satellite relay ended: %s", exc or "EOF")
        else:
            _LOGGER.info("wake service -> satellite relay ended: EOF")

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
