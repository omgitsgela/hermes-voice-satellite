"""The lease: run a conversation on a device that dialled in.

The conversation loop was written against a satellite *client*: something this host dials at a known address
and then drives. The inversion (issue #61) turns that around, because the device owns the socket and we own
the conversation. This module presents the surface the loop already uses over a
`registry.SatelliteConnection`, so nothing above it needs to know which shape it is talking to.

It **subclasses `SatelliteClient` and overrides only the connection lifecycle and the two I/O primitives
(`send`, `next_event`)**. Everything whose behaviour is worth preserving is inherited unchanged: the describe
handshake, playback pacing and resampling, the wait for `played`, the TTS stream. That is deliberate. A
second, parallel implementation of playback is a second thing to keep in step, and this project already has
one defect that came from two code paths disagreeing about who owns a socket.

What a lease cannot do is reconnect. A dialled satellite can be re-dialled; a leased one connects when the
device decides to. So `reconnect()` re-arms the device over the link it already owns, and losing the link
means waiting for a device to arrive, which `bridge/loop.py` handles.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from wyoming.audio import AudioChunk
from wyoming.event import Event

from bridge.registry import SatelliteConnection
from bridge.satellite import SatelliteClient

_LOGGER = logging.getLogger(__name__)


class LeasedSatellite(SatelliteClient):
    """A satellite we did not dial: a device that dialled us and is leased to this conversation."""

    def __init__(self, connection: SatelliteConnection, *, log_events: bool = True) -> None:
        peer_host, _, peer_port = (connection.peer or "").rpartition(":")
        try:
            port = int(peer_port)
        except ValueError:
            port = 10700
        super().__init__(peer_host or connection.device_id, port, log_events=log_events)

        self.connection = connection
        self.device_id = connection.device_id
        self.generation = connection.generation
        # One read-ahead read, never cancelled mid-flight. See next_event for why.
        self._pending_get: Optional[asyncio.Task] = None

    @property
    def eof(self) -> bool:
        """True when the device's link is gone. The loop checks this to tell a timeout from a death."""
        return self.connection.closed

    # -- lifecycle ---------------------------------------------------------------

    async def connect(self) -> None:
        """Nothing to dial: the device already dialled us and the registry holds its socket."""
        _LOGGER.info(
            "leased device %s (generation %s) from %s",
            self.device_id, self.generation, self.connection.peer,
        )

    async def close(self) -> None:
        self._cancel_pending()
        await self.connection.close()

    async def reconnect(self) -> None:
        """Re-arm the device on the link it already owns.

        The loop calls this to refresh an idle link. For a dialled satellite that means a new socket; here
        the socket is the device's, and what is actually needed is telling it we are ready again, which is
        what re-arms its wake word. Sending `run-satellite` is the whole job.
        """
        _LOGGER.info("re-arming leased device %s (generation %s)", self.device_id, self.generation)
        await self.run()

    # -- the two primitives everything else is built on --------------------------

    async def send(self, event: Event) -> None:
        ok = await self.connection.send(event)
        if not ok:
            # Match the dialled client, where writing on a dead socket raises: callers depend on the error
            # to stop waiting for an answer that cannot come.
            raise ConnectionError(f"the link to {self.device_id} is closed")
        if self._log_events and not AudioChunk.is_type(event.type):
            _LOGGER.info("us -> %s : %s", self.device_id, event.type)

    async def next_event(self, timeout: float) -> Optional[Event]:
        """Next event from the device, or None on timeout. `.eof` distinguishes a dead link.

        The blocking read is kept as a single long-lived task and never cancelled, only ever consumed. The
        obvious implementation, `wait_for(queue.get(), slice)`, cancels the read on every slice boundary, and
        a queue read cancelled after an item has been handed to it loses that item. A lost audio chunk is a
        click in the middle of a sentence; a lost `played` event costs the next answer its pacing. So the
        wait races the pending read against the connection's closed signal instead, and a slice expiring
        simply leaves the read in place for the next call.
        """
        if timeout <= 0:
            return self._log_incoming(self.connection.get_nowait())

        if self._pending_get is None:
            if self.connection.closed:
                # A closed link can still hold events that arrived before it ended. Hand those over before
                # reporting the end, or the last `played` of a conversation is thrown away with the socket
                # and the next answer loses its pacing.
                return self._log_incoming(self.connection.get_nowait())
            self._pending_get = asyncio.ensure_future(self.connection.get())
        elif self._pending_get.done():
            # An event that arrived while nobody was waiting. Consume it rather than starting another read.
            return self._consume_pending()

        closed = asyncio.ensure_future(self.connection.closed_event.wait())
        try:
            done, _ = await asyncio.wait(
                {self._pending_get, closed}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not closed.done():
                closed.cancel()

        if self._pending_get in done:
            return self._consume_pending()
        if closed in done:
            # The device went away. An event that arrived in the same instant wins over reporting the end,
            # so nothing is dropped on the way out.
            if self._pending_get.done():
                return self._consume_pending()
            _LOGGER.info("device %s (generation %s) closed the link", self.device_id, self.generation)
            return None
        return None     # a slice expired with nothing to hand over: the read stays pending

    # -- internals ---------------------------------------------------------------

    def _consume_pending(self) -> Optional[Event]:
        task, self._pending_get = self._pending_get, None
        if task is None:
            return None
        try:
            return self._log_incoming(task.result())
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - a dead read is not an event
            return None

    def _cancel_pending(self) -> None:
        task, self._pending_get = self._pending_get, None
        if task is not None and not task.done():
            task.cancel()

    def _log_incoming(self, event: Optional[Event]) -> Optional[Event]:
        if self._log_events and event is not None and not AudioChunk.is_type(event.type):
            _LOGGER.info("%s -> us : %s", self.device_id, event.type)
        return event
