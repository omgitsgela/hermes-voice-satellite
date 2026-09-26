#!/usr/bin/env python3
"""Outbound transport for a satellite that dials the bridge (issue #61, migration phase 3).

The device stops being a Wyoming *server* and becomes a Wyoming *client*: it opens one connection to the
bridge, registers, and then carries everything over that socket - microphone audio, wake detections, the
transcript, playback, the LED events and the announcement path.

**Installable as a service, and the relay carries the audio.** `pi/systemd/outbound-satellite.service.in`
is the unit, `docs/outbound-link.md` is the install procedure, and `_serve_local()` is the relay: once
registered, this process connects to the `wyoming-satellite` service already running on the same device
and becomes a transparent pipe to it in both directions. That is deliberate - wake word, capture,
playback, the chime and the LEDs stay in the service that already does them well, so there is exactly one
code path that owns the audio device.

The connection, handshake, refusal path, reconnect behaviour, relay teardown and the configuration
control plane are all covered offline against a fake bridge (`scripts/test_outbound_runner.py`).
Deploying the relay on a real device and verifying it against real hardware is the part that still needs
the device, not writing it.

Design rules from the pre-implementation review, each visible in the code below:

1. **Never report ready before the bridge has accepted the registration.** `set_server()` on the current
   build flashes the connected LED the instant a socket exists, which would lie.
2. **The device owns reconnect**, with capped exponential backoff and jitter, and it re-resolves the
   endpoint on every attempt so a DNS change is survivable.
3. **A refused device must not hammer.** A refusal - wrong version, revoked credential, unknown device - is
   waited out on a much longer timer than a network failure, because retrying will never fix it.
4. **A stalled write is a dead link, not a hang.** Every write is bounded; the device awaiting a full TCP
   buffer would otherwise stop servicing its own microphone.
5. **One connection at a time**, always.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
import random
import ssl
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from wyoming.event import Event, async_read_event, async_write_event

try:  # package import in tests; sibling import when systemd executes this file directly
    from .config_agent import CONFIG_DESIRED_EVENT, DeviceConfigAgent
except ImportError:  # pragma: no cover - exercised by the installed script entrypoint
    from config_agent import CONFIG_DESIRED_EVENT, DeviceConfigAgent

try:
    from .device_apply import make_apply_adapter
    from .volume_control import AlsaVolume
except ImportError:  # pragma: no cover - exercised by the installed script entrypoint
    from device_apply import make_apply_adapter
    from volume_control import AlsaVolume

log = logging.getLogger("pi.outbound")

#: Must match the bridge. The bridge refuses a device that speaks a different version, and says so.
PROTOCOL_VERSION = 1

REGISTER_EVENT = "hermes.register"
REGISTER_OK_EVENT = "hermes.register.ok"
REGISTER_REFUSED_EVENT = "hermes.register.refused"


class RegistrationRefused(Exception):
    """The bridge refused this device. Retrying will not help until a human changes something."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class LinkConfig:
    """Everything the device needs to reach its bridge. Deliberately small: this is the whole of the
    device's bootstrap configuration, which is the point of the inverted shape."""

    device_id: str
    host: str
    port: int = 10700
    name: str = ""
    area: str = ""
    firmware_version: str = ""
    capabilities: tuple[str, ...] = ()
    boot_id: str = ""
    protocol_version: int = PROTOCOL_VERSION
    connect_timeout: float = 10.0
    handshake_timeout: float = 10.0
    write_timeout: float = 10.0
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    refused_backoff: float = 300.0
    #: How long a connection must stay up before the reconnect backoff is reset. Without this, a bridge
    #: that accepts the handshake and then immediately drops produces a tight retry loop forever, because
    #: every attempt looks successful (review finding, issue #63).
    healthy_after: float = 30.0
    ssl_context: Optional[ssl.SSLContext] = None
    #: Where this device's own Wyoming satellite listens. The relay pipes the outbound link to it, so the
    #: audio stays in the service that already does it well rather than being reimplemented here.
    local_host: str = "127.0.0.1"
    local_port: int = 10700
    #: Relay to the local satellite. Off is only useful for a device with no local satellite to relay to.
    relay: bool = True
    #: How long to wait between attempts to reach the local satellite. On a boot this service and the
    #: satellite start together and the satellite is the slower of the two, so the first attempt usually
    #: loses the race.
    local_retry_seconds: float = 5.0
    config_state_path: pathlib.Path = pathlib.Path.home() / ".local/state/hermes-voice-satellite/config.json"

    def registration_payload(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "area": self.area,
            "firmware_version": self.firmware_version,
            "protocol_version": self.protocol_version,
            "capabilities": list(self.capabilities),
            "boot_id": self.boot_id,
        }


@dataclass
class LinkStats:
    attempts: int = 0
    registrations: int = 0
    refusals: int = 0
    events_received: int = 0
    events_sent: int = 0
    write_failures: int = 0
    last_error: str = ""
    last_backoff: float = 0.0


def load_config_from_env(environ: Optional[Dict[str, str]] = None) -> LinkConfig:
    """Read the device's bootstrap configuration.

    The device carries network credentials, the bridge endpoint and its own identity; everything else
    (name, voice, wake word, LED behaviour) is policy and lives on the bridge.
    """
    env = dict(os.environ if environ is None else environ)
    device_id = env.get("VOICE_DEVICE_ID", "").strip()
    host = env.get("VOICE_BRIDGE_HOST", "").strip()
    if not device_id or not host:
        raise SystemExit(
            "VOICE_DEVICE_ID and VOICE_BRIDGE_HOST are required: this device cannot register without "
            "knowing what it is and where to dial"
        )
    return LinkConfig(
        device_id=device_id,
        host=host,
        port=int(env.get("VOICE_BRIDGE_PORT", "10700")),
        name=env.get("VOICE_DEVICE_NAME", ""),
        area=env.get("VOICE_DEVICE_AREA", ""),
        firmware_version=env.get("VOICE_FIRMWARE_VERSION", ""),
        capabilities=tuple(c for c in env.get("VOICE_CAPABILITIES", "led,speaker,microphone").split(",") if c),
        boot_id=pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if pathlib.Path("/proc/sys/kernel/random/boot_id").exists() else "",
        local_host=env.get("VOICE_LOCAL_SATELLITE_HOST", "127.0.0.1").strip() or "127.0.0.1",
        local_port=int(env.get("VOICE_LOCAL_SATELLITE_PORT", "10700")),
        relay=env.get("VOICE_RELAY", "1").strip().lower() not in ("0", "false", "no", "off"),
        config_state_path=pathlib.Path(
            env.get("VOICE_DEVICE_CONFIG_STATE", "").strip()
            or (pathlib.Path.home() / ".local/state/hermes-voice-satellite/config.json")
        ),
        ssl_context=_ssl_context_from_env(env),
    )


def _ssl_context_from_env(env: Dict[str, str]) -> Optional[ssl.SSLContext]:
    """A TLS context when the deployment needs one, or None for a plain local connection.

    `VOICE_BRIDGE_TLS_CA` sets the CA to validate the bridge against. The client certificate and key are
    optional: they are what mTLS identity needs, and a device without them is only allowed on a listener
    that does not demand a credential.
    """
    ca = env.get("VOICE_BRIDGE_TLS_CA", "").strip()
    if not ca:
        return None
    context = ssl.create_default_context(cafile=ca)
    cert = env.get("VOICE_BRIDGE_TLS_CERT", "").strip()
    key = env.get("VOICE_BRIDGE_TLS_KEY", "").strip()
    if cert and key:
        context.load_cert_chain(certfile=cert, keyfile=key)
    return context


class BridgeLink:
    """One connection to the bridge, kept alive across reconnects."""

    def __init__(
        self,
        config: LinkConfig,
        *,
        on_event: Optional[Callable[[Event], Any]] = None,
        on_registered: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_disconnected: Optional[Callable[[str], Any]] = None,
        stats: Optional[LinkStats] = None,
        config_agent: Optional[DeviceConfigAgent] = None,
        alsa_volume: Optional[AlsaVolume] = None,
    ) -> None:
        self.config = config
        self.on_event = on_event
        self.on_registered = on_registered
        self.on_disconnected = on_disconnected
        self.stats = stats or LinkStats()
        if config_agent is not None:
            self.config_agent = config_agent
        else:
            # Wire the hardware adapter in. Without this the agent validates, persists and
            # reports - and never touches the device, so every setting would be staged
            # forever while looking healthy. The adapter's own tests cannot catch that; only
            # constructing the runner the way production does can.
            self.config_agent = DeviceConfigAgent(
                config.config_state_path,
                device_id=config.device_id,
                apply=make_apply_adapter(alsa_volume or AlsaVolume.from_calibration()),
            )

        self.connected = False
        self.registered = False
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._write_lock = asyncio.Lock()
        self._stopping = False
        self._local_writer: Optional[asyncio.StreamWriter] = None
        self._generation = 0
        self._backoff_index = 0
        self._rng = random.Random()

    # -- public API ------------------------------------------------------------

    @property
    def generation(self) -> int:
        return self._generation

    async def run(self) -> None:
        """Connect, register, serve, and reconnect until stopped. Never returns on its own."""
        while not self._stopping:
            started = time.monotonic()
            try:
                await self._connect_and_serve()
                ended = "the bridge closed the connection"
            except RegistrationRefused as exc:
                self.stats.refusals += 1
                self.stats.last_error = exc.reason
                log.error(
                    "the bridge refused this device: %s. Waiting %.0fs before asking again: retrying "
                    "will not change the answer until someone acts on the bridge.",
                    exc.reason, self.config.refused_backoff,
                )
                await self._sleep(self.config.refused_backoff)
                continue
            except (OSError, ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
                ended = f"{type(exc).__name__}: {exc}"
                self.stats.last_error = ended
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a transport bug must not kill the device
                ended = f"unexpected {type(exc).__name__}: {exc}"
                self.stats.last_error = ended
                log.exception("unexpected failure in the bridge link")

            # Reset the backoff only after a connection has actually stayed up. Resetting it on the
            # handshake alone means an accept-then-drop bridge keeps every retry at the base interval
            # (review finding, issue #63).
            if time.monotonic() - started >= self.config.healthy_after:
                if self._backoff_index:
                    log.info("bridge link was healthy for %.0fs; resetting the reconnect backoff",
                             self.config.healthy_after)
                self._backoff_index = 0

            self._teardown()
            if self.on_disconnected is not None:
                await _maybe_await(self.on_disconnected(ended))
            delay = self._next_backoff()
            log.warning("bridge link down (%s); retrying in %.1fs", ended, delay)
            await self._sleep(delay)

    async def stop(self) -> None:
        self._stopping = True
        self._teardown()

    async def send(self, event: Event) -> bool:
        """Send one event, bounded. A stalled write closes the link rather than hanging the device."""
        if not self.connected or self.writer is None:
            return False
        async with self._write_lock:
            writer = self.writer
            if writer is None:
                return False
            try:
                await asyncio.wait_for(
                    _write_event(event, writer), timeout=self.config.write_timeout
                )
            except (asyncio.TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
                self.stats.write_failures += 1
                self.stats.last_error = f"write failed: {type(exc).__name__}"
                log.warning(
                    "giving up on this connection: the write did not complete in %.0fs (%s). A device "
                    "that waits on a full TCP buffer stops servicing its own microphone.",
                    self.config.write_timeout, type(exc).__name__,
                )
                # Tear down only the socket this write was using. If the run loop has already replaced it,
                # this failure belongs to a dead generation and must not close the live connection
                # (review finding, issue #64).
                self._teardown(writer)
                return False
            self.stats.events_sent += 1
            return True

    # -- internals -------------------------------------------------------------

    async def _connect_and_serve(self) -> None:
        self.stats.attempts += 1
        # Re-resolve every attempt: a stale address must not persist for the process lifetime.
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.config.host, self.config.port, ssl=self.config.ssl_context),
            timeout=self.config.connect_timeout,
        )
        self._enable_tcp_keepalive()
        self.connected = True

        await self._handshake()

        self.registered = True
        self._generation += 1
        self.stats.registrations += 1
        # The backoff is deliberately NOT reset here: a connection that registers and then dies must keep
        # accumulating backoff, or an accept-then-drop bridge produces a tight retry loop (issue #63). The
        # reset happens in run() only after the link has been healthy for healthy_after seconds.
        log.info(
            "registered with the bridge as %s (generation %s)", self.config.device_id, self._generation
        )
        if self.on_registered is not None:
            await _maybe_await(self.on_registered(self.config.registration_payload()))

        if self.config.relay:
            # Carry what the bridge says to this device's own satellite, and what the satellite says back.
            # See _serve_local for why this is a pipe and not a second implementation of the audio.
            await self._serve_local()
        else:
            await self._pump()

    def _enable_tcp_keepalive(self) -> None:
        """Make an idle link notice that the bridge host vanished.

        Without keepalive, a powered-off bridge can leave the device-side socket in ESTABLISHED for the
        kernel's multi-hour default. The reader then sees no EOF, the reconnect loop never runs, and the
        device remains registered only in its own imagination. Match the host-side liveness window: after
        15 idle seconds, three probes five seconds apart surface a dead peer in roughly 30 seconds.
        """
        import socket as _socket

        try:
            writer = self.writer
            sock = writer.get_extra_info("socket") if writer is not None else None
            if sock is None:
                log.warning("could not reach the bridge socket, so TCP keepalive is off")
                return
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
            for name, value in (("TCP_KEEPIDLE", 15), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
                option = getattr(_socket, name, None)
                if option is not None:
                    sock.setsockopt(_socket.IPPROTO_TCP, option, value)
            log.info("TCP keepalive on for the bridge link (15s idle, 3 probes 5s apart)")
        except Exception as exc:  # noqa: BLE001 - liveness tuning must not block a usable link
            log.warning("could not enable TCP keepalive on the bridge link: %s", exc)

    async def _handshake(self) -> None:
        """describe -> info -> register -> ok, with every step bounded."""
        reader = self._require_reader()
        first = await asyncio.wait_for(async_read_event(reader), self.config.handshake_timeout)
        if first is not None and first.type != "describe":
            log.info("bridge opened with %s rather than describe", first.type)

        await self._send_raw(Event(type="info", data=self._info_payload()))

        reply = await self._send_and_wait(
            Event(type=REGISTER_EVENT, data=self.config.registration_payload()),
            expect=(REGISTER_OK_EVENT, REGISTER_REFUSED_EVENT),
        )
        if reply.type == REGISTER_REFUSED_EVENT:
            raise RegistrationRefused(str(reply.data.get("reason") or "no reason given"))

    def _info_payload(self) -> Dict[str, Any]:
        satellite: Dict[str, Any] = {"name": self.config.name}
        if self.config.area:
            satellite["area"] = self.config.area
        if self.config.firmware_version:
            satellite["version"] = self.config.firmware_version
        if self.config.capabilities:
            satellite["active_wake_words"] = []
        return {"satellite": satellite}

    async def _send_and_wait(self, event: Event, *, expect: tuple[str, ...]) -> Event:
        """Write an event and read until one of the expected types arrives."""
        reader = self._require_reader()
        await self._send_raw(event)
        deadline = time.monotonic() + self.config.handshake_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"no {expect} within the handshake timeout")
            incoming = await asyncio.wait_for(async_read_event(reader), remaining)
            if incoming is None:
                raise ConnectionError("the bridge closed the connection during the handshake")
            self.stats.events_received += 1
            if incoming.type in expect:
                return incoming
            log.debug("ignoring %s while waiting for %s", incoming.type, expect)

    async def _send_raw(self, event: Event) -> None:
        writer = self.writer
        if writer is None:
            raise ConnectionError("not connected")
        await asyncio.wait_for(_write_event(event, writer), timeout=self.config.write_timeout)

    async def _pump(self) -> None:
        reader = self._require_reader()
        while True:
            event = await async_read_event(reader)
            if event is None:
                return
            self.stats.events_received += 1
            if event.type == "ping":
                await self.send(Event(type="pong"))
                continue
            if event.type == CONFIG_DESIRED_EVENT:
                report = await self.config_agent.apply_event(event)
                if not await self.send(report):
                    raise ConnectionError("could not return the configuration report")
                continue
            if self.on_event is not None:
                await _maybe_await(self.on_event(event))

    # -- the local relay ---------------------------------------------------------

    async def _serve_local(self) -> None:
        """Pipe the outbound link to this device's own Wyoming satellite.

        The audio lives in the satellite service already running on this device: wake word, microphone
        capture, playback, the LEDs, all of it tuned here over months. Reimplementing that state machine on
        the far side of a socket would mean two copies to keep in step, and this project has already paid
        once for two code paths disagreeing about who owns a socket. So the link becomes a transparent pipe
        to the existing service, and the bridge sees exactly the satellite it has always talked to, with one
        difference: this device opened the connection instead of waiting to be dialled.

        Returns when either end stops, which the caller treats as the link ending and reconnects from.
        """
        # Read the bridge while the local service is still booting. Buffer a bounded
        # number of ordinary events; control events and EOF must not wait for it.
        pending: asyncio.Queue[Event] = asyncio.Queue(maxsize=256)

        def enqueue(event: Event) -> None:
            try:
                pending.put_nowait(event)
            except asyncio.QueueFull as exc:
                raise ConnectionError("local satellite relay backlog is full") from exc

        self.on_event = enqueue
        tasks = {
            asyncio.create_task(self._pump()),
            asyncio.create_task(self._serve_local_connection(pending)),
        }
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            self.on_event = None

    async def _serve_local_connection(self, pending: asyncio.Queue[Event]) -> None:
        reader = writer = None
        while reader is None:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.config.local_host, self.config.local_port),
                    timeout=self.config.connect_timeout,
                )
            except (OSError, asyncio.TimeoutError) as exc:
                # Nothing is listening yet, which is the normal case on a boot: this service and the satellite
                # start together and the satellite is the slower of the two. Keep the bridge link open and
                # keep trying. Holding a link that can never relay anything, which is what this used to do
                # after one failed attempt, leaves the bridge talking to a device that says nothing.
                log.warning(
                    "no local satellite at %s:%s yet (%s); retrying in %.0fs",
                    self.config.local_host, self.config.local_port, exc, self.config.local_retry_seconds,
                )
                await self._sleep(self.config.local_retry_seconds)
                if not self.connected:
                    # The bridge link died while we waited. Let the caller reconnect both ends together.
                    return

        self._local_writer = writer
        log.info("relaying to the local satellite at %s:%s",
                 self.config.local_host, self.config.local_port)
        upstream = asyncio.create_task(self._local_to_bridge(reader))
        async def forward_pending() -> None:
            while True:
                await self.relay_event_to_local(await pending.get())

        downstream = asyncio.create_task(forward_pending())
        tasks = {upstream, downstream}
        try:
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            # Propagate a relay error to the run loop. A clean EOF simply returns,
            # and either outcome tears down both legs so they reconnect together.
            for task in done:
                await task
        finally:
            self._local_writer = None
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            try:
                if writer is not None:
                    writer.close()
                    with contextlib.suppress(BaseException):
                        await writer.wait_closed()
            except Exception:  # noqa: BLE001 - best effort on a socket that is already ending
                pass

    async def _local_to_bridge(self, reader: asyncio.StreamReader) -> None:
        """Everything the satellite says goes up the outbound link, until either end stops."""
        while True:
            event = await async_read_event(reader)
            if event is None:
                log.info("the local satellite closed the connection")
                return
            if not await self.send(event):
                return

    async def relay_event_to_local(self, event: Event) -> None:
        """One event from the bridge, written straight to the local satellite."""
        writer = self._local_writer
        if writer is None:
            return
        try:
            await asyncio.wait_for(_write_event(event, writer), timeout=self.config.write_timeout)
        except (ConnectionError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            # End the paired relay rather than silently dropping every later event.
            # _serve_local races both directions and will reconnect both legs together.
            log.warning("could not write to the local satellite (%s); dropping that connection",
                        type(exc).__name__)
            if self._local_writer is writer:
                self._local_writer = None
            with contextlib.suppress(Exception):
                writer.close()
            raise ConnectionError("the local satellite write failed") from exc

    def _require_reader(self) -> asyncio.StreamReader:
        if self.reader is None:
            raise ConnectionError("not connected")
        return self.reader

    def _teardown(self, writer: Optional[asyncio.StreamWriter] = None) -> None:
        """Close the link, or just the dead socket a failed write was using.

        With no argument this is the run loop's own teardown: the current connection has ended. With a
        writer argument it is a write failure, and it must only clear the link when that writer is still
        the current one. Otherwise a stale failure from generation N closes generation N+1, which the run
        loop has already installed (review finding, issue #64).
        """
        if writer is not None and writer is not self.writer:
            try:
                writer.close()
            except Exception:  # pragma: no cover - best effort
                pass
            log.debug("a stale write failed on a superseded socket; the live connection is untouched")
            return

        self.connected = False
        self.registered = False
        current, self.writer = self.writer, None
        self.reader = None
        if current is not None:
            try:
                current.close()
            except Exception:  # pragma: no cover - best effort
                pass

    def _next_backoff(self) -> float:
        """Full jitter: uniform over (0, cap], with the cap doubling per consecutive failure."""
        cap = min(self.config.backoff_max, self.config.backoff_base * (2 ** self._backoff_index))
        self._backoff_index = min(self._backoff_index + 1, 16)
        delay = self._rng.uniform(max(cap / 4, 0.05), cap)
        self.stats.last_backoff = delay
        return delay

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            raise


async def _write_event(event: Event, writer: asyncio.StreamWriter) -> None:
    await async_write_event(event, writer)
    await writer.drain()


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


def main() -> int:
    """Run the link. This is what the installed systemd unit executes."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config_from_env()
    link = BridgeLink(config)
    try:
        asyncio.run(link.run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
