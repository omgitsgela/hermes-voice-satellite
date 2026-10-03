"""Device registry for the inverted connection shape (issue #61, migration phase 2).

**Wired into the bridge behind `VOICE_REGISTRY_ENABLED`, off by default.** A registered device is
leaseable by the voice loop and carries real conversations over the leased connection
(`bridge/lease.py`, acquired in `bridge/loop.py`). The registry also carries the configuration control
plane: desired state goes down, reported actual state comes back (`hermes.config.desired` /
`hermes.config.reported`), and those control events are handled out of band so they never enter the
audio queue.

Everything here is exercised by `scripts/test_registry.py`.

What it provides:

- `DeviceIdentity` — the authenticated identity of a satellite, and the validation of what a device may
  claim about itself.
- `SatelliteConnection` — one accepted socket, one generation, one bounded ingress queue, and typed waiters.
  The upstream `AsyncEventHandler` loop is the single reader, so "one reader per socket" is a property of
  the design rather than a rule someone has to remember.
- `DeviceSlot` — the state that must survive a reconnect: identity, metadata, desired state, playback lock,
  active-turn gate, last seen.
- `SatelliteRegistry` — generation-safe install and removal, with an explicit duplicate policy.
- `RegistryServer` — a Wyoming TCP listener whose handler registers a device and then dispatches, with an
  optional `on_control_event` hook for non-audio control-plane events.

Design rules this enforces, each from the pre-implementation review:

1. A connection is known by a monotonic **generation**; removal only happens if the generation still
   matches, so a device that reconnects cannot be removed by its own stale handler.
2. **Identity is not a claim.** `info.satellite.name` is metadata. If the transport authenticated the peer,
   the registration payload must agree with the authenticated identity or it is refused.
3. **Never block the reader.** `deliver()` is synchronous and bounded; the reader loop does no work beyond
   routing. Audio arrives at roughly 32 KB/s and a stalled reader becomes TCP backpressure on the device.
4. **Queue limits are in bytes as well as events**, because audio payloads dominate.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import pathlib
import re
import ssl
import time
from functools import partial
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from wyoming.event import Event, async_write_event
from wyoming.server import AsyncEventHandler, AsyncTcpServer, HandlerFactory

from bridge.net import enable_tcp_keepalive

log = logging.getLogger("bridge.registry")

#: Bumped when the registration contract changes, so an old device is refused clearly instead of
#: half-working.
PROTOCOL_VERSION = 1

REGISTER_EVENT = "hermes.register"
REGISTER_OK_EVENT = "hermes.register.ok"
REGISTER_REFUSED_EVENT = "hermes.register.refused"
CONFIG_DESIRED_EVENT = "hermes.config.desired"
CONFIG_REPORTED_EVENT = "hermes.config.reported"

#: A device that connects and claims nothing is dropped after this long.
REGISTRATION_TIMEOUT_SECONDS = 10.0

# Registration field limits. These are not decoration: without them a peer that can reach the listener
# can create arbitrary identities and grow the registry without bound (review finding on #61).
MAX_DEVICE_ID_CHARS = 64
MAX_NAME_CHARS = 64
MAX_BOOT_ID_CHARS = 128
MAX_CAPABILITIES = 32
MAX_CAPABILITY_CHARS = 32
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class CredentialConfigError(Exception):
    """The credential configuration is present but unusable.

    Deliberately distinct from "there is no credential configuration": the first must stop the listener
    from starting (fail closed), the second is only allowed with an explicit trusted-network opt-in. A
    typo or a partial write in a credential file must never quietly downgrade a listener to believing
    whatever a device claims.
    """


def load_credential_lookup(
    path: pathlib.Path, *, allow_unauthenticated: bool = False
) -> Optional[Callable[[str], Optional[str]]]:
    """Build the fingerprint -> device_id lookup from a JSON file, or None for trusted-network mode.

    - File absent and `allow_unauthenticated` is True: returns None, and the caller must warn loudly.
    - File absent and `allow_unauthenticated` is False: raises `CredentialConfigError`. Absence is not
      consent to run unauthenticated.
    - File present but unreadable or malformed: raises `CredentialConfigError`, always, so a broken file
      cannot open the door.
    - File present and valid: returns the lookup. An unknown fingerprint is refused by the caller.
    """
    if not path.is_file():
        if allow_unauthenticated:
            return None
        raise CredentialConfigError(
            f"no credential file at {path}, and unauthenticated registration was not explicitly enabled. "
            "Either provide the file or set VOICE_REGISTRY_ALLOW_UNAUTHENTICATED=1 on a trusted network."
        )
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise CredentialConfigError(
            f"credential file {path} is present but unusable: {type(exc).__name__}: {exc}. Refusing to "
            "start the listener rather than accepting self-claimed identities."
        ) from exc
    if not isinstance(raw, dict):
        raise CredentialConfigError(
            f"credential file {path} must be a JSON object mapping fingerprint to device id"
        )
    mapping = {str(k): str(v) for k, v in raw.items() if str(k).strip() and str(v).strip()}
    if not mapping:
        raise CredentialConfigError(f"credential file {path} contains no usable entries")

    def lookup(fingerprint: str) -> Optional[str]:
        return mapping.get(fingerprint)

    log.info("registry credentials loaded from %s: %d device(s)", path, len(mapping))
    return lookup


class TlsConfigError(Exception):
    """The listener's TLS configuration is present but unusable.

    The same principle as `CredentialConfigError`, for the same reason: a half-deployed certificate
    must stop the listener rather than leave it silently plaintext. Someone who installs a certificate
    and sees the bridge come up is entitled to assume the traffic is encrypted, and a listener that
    quietly stayed plain would be the worst outcome of all.
    """


def load_server_tls_context(
    cert_path: pathlib.Path, key_path: pathlib.Path, client_ca_path: pathlib.Path
) -> Optional[ssl.SSLContext]:
    """The registry listener's TLS context, or None when TLS is not configured.

    TLS is on when the certificate and its key both exist. That is what makes it the standard
    transport rather than a flag to remember: provisioning a certificate *is* the opt-in, and nobody
    can end up with certificates installed and an unencrypted listener without being told. Neither
    present is the plain local case, a socket on a network the household controls.

    Half a pair, or a certificate with no client CA, is refused. Both leave a listener that looks
    configured and is not, and the failure would only show up as a device that cannot connect.

    Mutually authenticated by construction. The credential is the client certificate's fingerprint, so
    a listener that let a peer skip its certificate would be authenticating nobody while appearing to
    do exactly that.
    """
    has_cert = cert_path.is_file()
    has_key = key_path.is_file()
    if not has_cert and not has_key:
        return None
    if has_cert and not has_key:
        raise TlsConfigError(
            f"certificate {cert_path} has no key at {key_path}. Refusing to start rather than run the "
            "listener unencrypted now that a certificate is deployed."
        )
    if has_key and not has_cert:
        raise TlsConfigError(
            f"key {key_path} has no certificate at {cert_path}. Refusing to start rather than run the "
            "listener unencrypted now that a key is deployed."
        )
    if not client_ca_path.is_file():
        raise TlsConfigError(
            f"no client CA at {client_ca_path}. The listener authenticates each device by the fingerprint "
            "of its certificate, which it cannot check without the CA that signed them."
        )
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # TLS 1.2 is the floor because the ESP32 satellites speak mbedTLS. A floor one end cannot meet
        # is a connection that fails in the field rather than a decision taken here.
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        context.load_verify_locations(cafile=str(client_ca_path))
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise TlsConfigError(
            f"TLS configuration is unusable: {type(exc).__name__}: {exc}"
        ) from exc
    context.verify_mode = ssl.CERT_REQUIRED
    log.info(
        "registry TLS configured: certificate %s, client CA %s, peer certificates required",
        cert_path, client_ca_path,
    )
    return context


class TlsAsyncTcpServer(AsyncTcpServer):
    """`wyoming.server.AsyncTcpServer` with a TLS context.

    The pinned wyoming release (1.10.2) creates its socket with `asyncio.start_server(host=..., port=...)`
    and no `ssl` argument, so TLS cannot be turned on through the library as it stands. Only the two
    methods that create the server are overridden here; the handler callback, the stop signal and
    `stop()` are inherited, so the handler lifecycle cannot drift away from the library's.
    """

    def __init__(self, host: str, port: int, *, ssl_context: Optional[ssl.SSLContext] = None) -> None:
        super().__init__(host, port)
        self.ssl_context = ssl_context

    async def start(self, handler_factory: HandlerFactory) -> None:
        """Start server without blocking."""
        handler_callback = partial(self._handler_callback, handler_factory)
        self._server = await asyncio.start_server(
            handler_callback, host=self.host, port=self.port, ssl=self.ssl_context
        )
        await self._server.start_serving()

    async def run(self, handler_factory: HandlerFactory) -> None:
        """Start server and block while it runs."""
        handler_callback = partial(self._handler_callback, handler_factory)
        self._server = await asyncio.start_server(
            handler_callback, host=self.host, port=self.port, ssl=self.ssl_context
        )
        self._register_stop_signal()
        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:
            # Server.close() cancels serve_forever(); only swallow the cancellation the stop signal caused.
            if not self._stop_requested:
                raise
        finally:
            self._unregister_stop_signal()


class RegistrationError(Exception):
    """A registration that must be refused, with a reason suitable for logging and for the device."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass(frozen=True)
class DeviceIdentity:
    """What a satellite is, once its claim has been checked against what the transport proved."""

    device_id: str
    name: str = ""
    area: str = ""
    firmware_version: str = ""
    protocol_version: int = PROTOCOL_VERSION
    capabilities: Tuple[str, ...] = ()
    boot_id: str = ""

    @classmethod
    def from_registration(cls, data: Dict[str, Any], authenticated_id: Optional[str] = None) -> "DeviceIdentity":
        """Validate a registration payload.

        `authenticated_id` is what the transport proved (for example a client-certificate fingerprint
        mapped to a device record). When it is present, the payload's own `device_id` must match it. When
        it is absent the device is accepted on its own word, which is only appropriate on a trusted
        network: the caller decides that, not this class.
        """
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            raise RegistrationError("no device_id in the registration payload")
        if len(device_id) > MAX_DEVICE_ID_CHARS:
            raise RegistrationError(f"device_id is longer than {MAX_DEVICE_ID_CHARS} characters")
        if not DEVICE_ID_PATTERN.match(device_id):
            raise RegistrationError(
                "device_id may contain only letters, digits, dot, dash and underscore"
            )
        if authenticated_id is not None and device_id != authenticated_id:
            raise RegistrationError(
                f"device_id {device_id!r} does not match the authenticated identity {authenticated_id!r}"
            )

        try:
            protocol_version = int(data.get("protocol_version", PROTOCOL_VERSION))
        except (TypeError, ValueError):
            raise RegistrationError("protocol_version is not an integer") from None
        if protocol_version != PROTOCOL_VERSION:
            raise RegistrationError(
                f"protocol version {protocol_version} is not supported "
                f"(this bridge speaks {PROTOCOL_VERSION})"
            )

        # The contract says "a list of strings", so check that rather than coercing whatever arrives: a
        # dict would otherwise register as its keys, and an integer as a string nobody meant to send.
        capabilities = data.get("capabilities") or ()
        if isinstance(capabilities, (str, bytes, dict)) or not isinstance(capabilities, Iterable):
            raise RegistrationError("capabilities must be a list of strings")
        capabilities = tuple(capabilities)
        if len(capabilities) > MAX_CAPABILITIES:
            raise RegistrationError(f"more than {MAX_CAPABILITIES} capabilities is not accepted")
        for entry in capabilities:
            if not isinstance(entry, str) or len(entry) > MAX_CAPABILITY_CHARS:
                raise RegistrationError(
                    f"capability {entry!r} is not a string of at most {MAX_CAPABILITY_CHARS} characters"
                )

        for field_name, limit in (("name", MAX_NAME_CHARS), ("area", MAX_NAME_CHARS),
                                  ("boot_id", MAX_BOOT_ID_CHARS), ("firmware_version", MAX_NAME_CHARS)):
            value = data.get(field_name)
            if value is not None and len(str(value)) > limit:
                raise RegistrationError(f"{field_name} is longer than {limit} characters")

        return cls(
            device_id=device_id,
            name=str(data.get("name") or ""),
            area=str(data.get("area") or ""),
            firmware_version=str(data.get("firmware_version") or ""),
            protocol_version=protocol_version,
            capabilities=capabilities,
            boot_id=str(data.get("boot_id") or ""),
        )

    def as_dict(self) -> Dict[str, Any]:
        out = dataclasses.asdict(self)
        out["capabilities"] = list(self.capabilities)
        return out


@dataclasses.dataclass
class IngressLimits:
    """Bounded ingress. Audio dominates the byte count, so both limits matter."""

    max_events: int = 64
    max_bytes: int = 256 * 1024


@dataclasses.dataclass
class ConnectionStats:
    events_received: int = 0
    bytes_received: int = 0
    events_dropped: int = 0
    bytes_dropped: int = 0
    events_sent: int = 0


def describe_peer(writer: asyncio.StreamWriter) -> str:
    try:
        peer = writer.get_extra_info("peername")
    except Exception:  # pragma: no cover - transport specific
        return "unknown"
    if isinstance(peer, tuple) and peer:
        return f"{peer[0]}:{peer[1]}"
    return str(peer or "unknown")


class SatelliteConnection:
    """One accepted socket: one generation, one reader, one bounded queue, typed waiters."""

    def __init__(
        self,
        device_id: str,
        generation: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        limits: Optional[IngressLimits] = None,
    ) -> None:
        self.device_id = device_id
        self.generation = generation
        self.reader = reader
        self.writer = writer
        self.limits = limits or IngressLimits()
        self.created_at = time.time()
        self.closed = False
        self.stats = ConnectionStats()
        self.identity: Optional[DeviceIdentity] = None
        self.peer = describe_peer(writer)

        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self.limits.max_events)
        # Set when this connection ends, so a reader blocked on the queue can be woken without cancelling a
        # queue read. Cancelling one after an item has been handed over loses that item, and a lost audio
        # chunk or a lost `played` event is a real defect rather than a theoretical one.
        self.closed_event = asyncio.Event()
        self._queued_bytes = 0
        self._waiters: List[Tuple[str, Optional[Callable[[Event], bool]], asyncio.Future]] = []
        self._write_lock = asyncio.Lock()

    # -- ingress ---------------------------------------------------------------

    def deliver(self, event: Event) -> bool:
        """Route one event. Synchronous and non-blocking by design: this runs in the reader loop."""
        size = len(event.payload or b"")
        self.stats.events_received += 1
        self.stats.bytes_received += size

        # Typed waiters first: a waiter asking for this event type takes it and the queue is not involved,
        # which is what stops a control event (Played) being swallowed by a streaming consumer.
        for index, (event_type, predicate, future) in enumerate(list(self._waiters)):
            if event.type != event_type:
                continue
            if predicate is not None and not predicate(event):
                continue
            self._waiters.pop(index)
            if not future.done():
                future.set_result(event)
            return True

        if self._queue.full() or self._queued_bytes + size > self.limits.max_bytes:
            # Explicit overflow policy: drop the newest event rather than grow without limit, and count it
            # so the caller can fail the affected turn instead of pretending nothing happened.
            self.stats.events_dropped += 1
            self.stats.bytes_dropped += size
            log.warning(
                "device %s generation %s: ingress full (%d events, %d bytes queued), dropped %s",
                self.device_id, self.generation, self._queue.qsize(), self._queued_bytes, event.type,
            )
            return False

        self._queue.put_nowait(event)
        self._queued_bytes += size
        return True

    async def get(self) -> Event:
        event = await self._queue.get()
        self._queued_bytes -= len(event.payload or b"")
        return event

    def get_nowait(self) -> Optional[Event]:
        try:
            event = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        self._queued_bytes -= len(event.payload or b"")
        return event

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    @property
    def queued_bytes(self) -> int:
        return self._queued_bytes

    async def wait_for(
        self,
        event_type: str,
        *,
        timeout: Optional[float] = None,
        predicate: Optional[Callable[[Event], bool]] = None,
    ) -> Optional[Event]:
        """Wait for one event of a type without competing with the streaming consumer.

        Returns the event, or None on timeout. Nothing is left behind on timeout: the waiter list is the
        only place a pending wait lives.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        waiter = (event_type, predicate, future)
        self._waiters.append(waiter)
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    # -- egress ---------------------------------------------------------------

    async def send(self, event: Event) -> bool:
        """Write one event. Serialised, so ordering is preserved without the caller holding a lock."""
        if self.closed:
            return False
        async with self._write_lock:
            if self.closed:
                return False
            try:
                await async_write_event(event, self.writer)
                await self.writer.drain()
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                await self.close()
                return False
            self.stats.events_sent += 1
            return True

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.closed_event.set()
        try:
            self.writer.close()
        except Exception:  # pragma: no cover - best effort
            pass
        for _, _, future in self._waiters:
            if not future.done():
                future.cancel()
        self._waiters.clear()


class DeviceSlot:
    """Per-device state that must survive a reconnect.

    Locks, sessions and gates belong here rather than on the process, or one room's conversation can block
    another room's announcement and a session rotation in the kitchen can reset the study.
    """

    def __init__(
        self,
        device_id: str,
        identity: Optional[DeviceIdentity] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self.device_id = device_id
        self.identity = identity
        self.session_id = session_id
        self.connection: Optional[SatelliteConnection] = None
        self.desired_state: Dict[str, Any] = {}
        self.active_turn_started_at: Optional[float] = None
        self.last_seen: Optional[float] = None
        self.generation_counter = 0
        # One playback lock per physical speaker: two devices may play at once, one device may not.
        self.playback_lock = asyncio.Lock()

    def next_generation(self) -> int:
        self.generation_counter += 1
        return self.generation_counter

    @property
    def ready(self) -> bool:
        """Connected and registered, which is not the same as TCP-connected."""
        return bool(self.connection and self.identity and not self.connection.closed)


class DuplicatePolicy:
    """What to do when a device that is already registered opens another connection."""

    REJECT = "reject"
    REPLACE = "replace"


class SatelliteRegistry:
    """The registry of connected satellites. Slots survive reconnects; connections do not."""

    def __init__(
        self,
        *,
        duplicate_policy: str = DuplicatePolicy.REPLACE,
        limits: Optional[IngressLimits] = None,
    ) -> None:
        if duplicate_policy not in (DuplicatePolicy.REJECT, DuplicatePolicy.REPLACE):
            raise ValueError(f"unknown duplicate policy {duplicate_policy!r}")
        self.duplicate_policy = duplicate_policy
        self.limits = limits or IngressLimits()
        self._slots: Dict[str, DeviceSlot] = {}
        self._lock = asyncio.Lock()

    # -- lookups ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._slots)

    def __contains__(self, device_id: object) -> bool:
        return device_id in self._slots

    def get(self, device_id: str) -> Optional[DeviceSlot]:
        return self._slots.get(device_id)

    def slots(self) -> List[DeviceSlot]:
        return list(self._slots.values())

    def connections(self, *, ready_only: bool = False) -> List[SatelliteConnection]:
        out = []
        for slot in self._slots.values():
            if slot.connection is None:
                continue
            if ready_only and not slot.ready:
                continue
            out.append(slot.connection)
        return out

    def connected_devices(self) -> List[str]:
        return [slot.device_id for slot in self._slots.values() if slot.ready]

    # -- lifecycle ------------------------------------------------------------

    async def slot_for(self, identity: DeviceIdentity) -> DeviceSlot:
        """The slot for a device, created the first time that device is seen.

        A fleet manager can later pre-create slots from stored desired state; this is where that state would
        be attached.
        """
        async with self._lock:
            slot = self._slots.get(identity.device_id)
            if slot is None:
                slot = DeviceSlot(device_id=identity.device_id, identity=identity)
                self._slots[identity.device_id] = slot
            return slot

    async def register(self, identity: DeviceIdentity, connection: SatelliteConnection) -> DeviceSlot:
        """Install a connection for a device, generation-safely.

        Raises `RegistrationError` when the policy is to reject and the device already has a live
        connection.
        """
        async with self._lock:
            slot = self._slots.get(identity.device_id)
            if slot is None:
                slot = DeviceSlot(device_id=identity.device_id, identity=identity)
                self._slots[identity.device_id] = slot

            previous = slot.connection
            if previous is not None and not previous.closed and previous is not connection:
                if self.duplicate_policy == DuplicatePolicy.REJECT:
                    raise RegistrationError("this device already has a live connection")
                log.info(
                    "device %s: replacing live connection generation %s with generation %s",
                    identity.device_id, previous.generation, connection.generation,
                )
                await previous.close()

            slot.identity = identity
            slot.connection = connection
            slot.last_seen = time.time()
            connection.identity = identity
            log.info(
                "device %s registered (%s%s), generation %s",
                identity.device_id,
                identity.name or "unnamed",
                f", area {identity.area}" if identity.area else "",
                connection.generation,
            )
            return slot

    def unregister(self, device_id: str, generation: int) -> bool:
        """Remove a connection **only if it is still the current generation**.

        This is the ABA guard: a device that reconnects before its previous handler's cleanup runs would
        otherwise be removed by that very cleanup, and the symptom is a device that keeps dropping with
        nothing in the log to explain it.
        """
        slot = self._slots.get(device_id)
        if slot is None or slot.connection is None:
            return False
        if slot.connection.generation != generation:
            log.info(
                "device %s: ignoring cleanup from stale generation %s (current %s)",
                device_id, generation, slot.connection.generation,
            )
            return False
        slot.connection = None
        slot.active_turn_started_at = None
        slot.last_seen = time.time()
        return True

    async def remove(self, device_id: str) -> bool:
        """Forget a device entirely (revocation, or an operator clearing a ghost)."""
        async with self._lock:
            slot = self._slots.pop(device_id, None)
        if slot is None:
            return False
        if slot.connection is not None:
            await slot.connection.close()
        return True

    async def close_all(self) -> None:
        for connection in self.connections():
            await connection.close()


class RegistrationHandler(AsyncEventHandler):
    """The per-connection handler: register the device, then route its events.

    `handle_event` never blocks on work: it validates the registration, then hands every later event to
    `SatelliteConnection.deliver`. That is how the single reader stays fast enough that the device does not
    stall on backpressure.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        registry: SatelliteRegistry,
        authenticated_id: Optional[str] = None,
        require_credential: bool = False,
        on_registered: Optional[Callable[[DeviceSlot], Any]] = None,
        on_control_event: Optional[Callable[[str, Event], Any]] = None,
        registration_timeout: float = REGISTRATION_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(reader, writer)
        self.registry = registry
        self.authenticated_id = authenticated_id
        self.require_credential = require_credential
        self.on_registered = on_registered
        self.on_control_event = on_control_event
        self.registration_timeout = registration_timeout
        self.connection: Optional[SatelliteConnection] = None
        self.slot: Optional[DeviceSlot] = None
        self.info: Dict[str, Any] = {}
        self._registration_watchdog: Optional[asyncio.Task] = None

    async def run(self) -> None:
        # Ask who they are, the standard way, then read until the socket ends. A device that says nothing
        # is dropped by the watchdog rather than held open: the deadline cannot live in handle_event, because
        # a silent socket never reaches it.
        try:
            await async_write_event(Event(type="describe"), self.writer)
            await self.writer.drain()
        except (ConnectionError, OSError):
            return
        self._registration_watchdog = asyncio.create_task(self._enforce_deadline())
        try:
            await super().run()
        except (ConnectionError, OSError) as exc:
            # A device that roams loses its route mid-read, so the read fails with EHOSTUNREACH rather
            # than returning a clean EOF. That is an ordinary disconnect, not a fault: the slot and the
            # lease are cleared by the registry and the lease layer, and this handler's only remaining
            # job is to end. Letting it escape made asyncio report "Task exception was never retrieved"
            # with a full traceback, because the library starts this task fire-and-forget and its
            # done-callback never reads the exception. Logged at info so it stays visible if the shape
            # ever changes. CancelledError is a BaseException, so a shutdown still propagates.
            log.info("registration connection ended: %s", exc or "peer unreachable")
        finally:
            self._registration_watchdog.cancel()

    async def _enforce_deadline(self) -> None:
        await asyncio.sleep(self.registration_timeout)
        if self.connection is None:
            await self._refuse("no registration within the deadline")

    async def handle_event(self, event: Event) -> bool:
        if self.connection is None:
            if event.type == "info":
                self.info = dict(event.data)
                return True
            if event.type == REGISTER_EVENT:
                return await self._register(event)
            # Anything else before registration is ignored rather than trusted; the watchdog ends a socket
            # that never registers.
            return True

        if event.type == CONFIG_REPORTED_EVENT:
            if self.on_control_event is not None:
                try:
                    result = self.on_control_event(self.connection.device_id, event)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:  # noqa: BLE001 - a bad report must not kill voice transport
                    log.warning("could not process configuration report from %s: %s",
                                self.connection.device_id, exc)
            return True
        self.connection.deliver(event)
        return True

    async def _register(self, event: Event) -> bool:
        # A listener that was configured with a credential lookup must not fall back to believing a device
        # that presents no usable credential: an unknown certificate is a refusal, not a trusted-LAN device.
        if self.require_credential and self.authenticated_id is None:
            await self._refuse("no recognised credential")
            return False
        try:
            identity = DeviceIdentity.from_registration(dict(event.data), self.authenticated_id)
        except RegistrationError as exc:
            await self._refuse(exc.reason)
            return False

        slot = await self.registry.slot_for(identity)
        connection = SatelliteConnection(
            identity.device_id, slot.next_generation(), self.reader, self.writer, self.registry.limits
        )
        try:
            self.slot = await self.registry.register(identity, connection)
        except RegistrationError as exc:
            # The refusal has to reach the device, so this must not close the socket first: a device that is
            # silently dropped looks like a network fault and retries forever.
            await self._refuse(exc.reason)
            return False

        if self._registration_watchdog is not None:
            self._registration_watchdog.cancel()

        self.connection = connection
        await connection.send(Event(type=REGISTER_OK_EVENT, data={
            "device_id": identity.device_id,
            "protocol_version": PROTOCOL_VERSION,
            "name": identity.name,
            "area": identity.area,
        }))
        if self.on_registered is not None:
            result = self.on_registered(self.slot)
            if asyncio.iscoroutine(result):
                await result
        return True

    async def _refuse(self, reason: str) -> None:
        log.warning("refused a registration from %s: %s", describe_peer(self.writer), reason)
        try:
            await async_write_event(
                Event(type=REGISTER_REFUSED_EVENT, data={"reason": reason}), self.writer
            )
            await self.writer.drain()
        except (ConnectionError, OSError):
            pass
        self.writer.close()

    async def disconnect(self) -> None:
        """Upstream calls this when the socket ends. Must be generation-safe."""
        if self.connection is None:
            return
        connection = self.connection
        connection.closed = True
        # Wake anything reading the connection: a lease blocked on the queue has to learn that the device
        # is gone, or it sits there until its own timeout expires.
        connection.closed_event.set()
        removed = self.registry.unregister(connection.device_id, connection.generation)
        log.info(
            "device %s disconnected (generation %s, %s)",
            connection.device_id, connection.generation,
            "slot cleared" if removed else "slot kept: a newer generation owns it",
        )


class RegistryServer:
    """A Wyoming listener that registers devices into a registry.

    `credential_lookup` maps something the transport proved (a client-certificate fingerprint, say) to a
    device id, and returns None for a credential it does not know. When the lookup is None the listener
    accepts any device that registers, which is only appropriate on a trusted network: this must not be
    exposed to the internet without authentication, because the protocol has none of its own.

    `ssl_context` turns on TLS for the listener, with peer certificates required. It is None for the
    plain local socket, which is a first-class choice on a network the household controls rather than a
    fallback: build it with `load_server_tls_context`.
    """

    def __init__(
        self,
        registry: SatelliteRegistry,
        host: str,
        port: int,
        *,
        credential_lookup: Optional[Callable[[str], Optional[str]]] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        on_registered: Optional[Callable[[DeviceSlot], Any]] = None,
        on_control_event: Optional[Callable[[str, Event], Any]] = None,
        registration_timeout: float = REGISTRATION_TIMEOUT_SECONDS,
    ) -> None:
        self.registry = registry
        self.host = host
        self.port = port
        self.credential_lookup = credential_lookup
        self.ssl_context = ssl_context
        self.on_registered = on_registered
        self.on_control_event = on_control_event
        self.registration_timeout = registration_timeout
        self._server: Optional[AsyncTcpServer] = None

    def _handler_factory(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> RegistrationHandler:
        # Keepalive on the accepted socket, for the same reason the dialled client has it: an idle link
        # has no timeout of its own, and a device that dies without closing leaves this end believing it
        # is connected. Measured 2026-09-26: the satellite was power-cycled and the bridge did not notice
        # at all — the registration and the lease were held for the whole outage, and only the returning
        # device displaced the stale socket. Applied per accepted connection, before anything reads.
        enable_tcp_keepalive(
            writer.get_extra_info("socket"),
            label=f"accepted device link from {describe_peer(writer)}",
            logger=log,
        )
        authenticated_id: Optional[str] = None
        known = True
        if self.credential_lookup is not None:
            fingerprint = peer_fingerprint(writer)
            authenticated_id = self.credential_lookup(fingerprint)
            if authenticated_id is None:
                log.warning("unknown credential %s from %s", fingerprint, describe_peer(writer))
        return RegistrationHandler(
            reader, writer,
            registry=self.registry,
            authenticated_id=authenticated_id,
            require_credential=self.credential_lookup is not None,
            on_registered=self.on_registered,
            on_control_event=self.on_control_event,
            registration_timeout=self.registration_timeout,
        )

    async def start(self) -> None:
        self._server = TlsAsyncTcpServer(self.host, self.port, ssl_context=self.ssl_context)
        await self._server.start(self._handler_factory)
        log.info(
            "registry listening on %s:%s%s",
            self.host, self.port,
            " with TLS (peer certificates required)" if self.ssl_context is not None else " without TLS",
        )

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None


def peer_fingerprint(writer: asyncio.StreamWriter) -> str:
    """Best-effort peer identity from the transport.

    With TLS this returns the SHA-256 of the peer's certificate, which is what `credential_lookup` maps to a
    device. Without TLS there is nothing to prove, and it returns `unauthenticated`: that is exactly why a
    public listener must not run without the lookup.
    """
    try:
        ssl_object = writer.get_extra_info("ssl_object")
    except Exception:  # pragma: no cover - transport specific
        ssl_object = None
    if ssl_object is not None:
        try:
            der = ssl_object.getpeercert(binary_form=True)
            if der:
                return "sha256:" + hashlib.sha256(der).hexdigest()
        except Exception:  # pragma: no cover
            pass
    return "unauthenticated"
