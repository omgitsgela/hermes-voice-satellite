"""Wyoming session for one satellite connection.

This is the protocol, not the transport, and the distinction matters: `connect()` dials a
device, but the device now dials US. The direction change did not fork the protocol -
`satellite_gateway/lease.py` subclasses `SatelliteClient` and overrides only the connection lifecycle
and the two I/O primitives (`send`, `next_event`), so this session logic runs unchanged over
a connection that arrived instead of one that was dialled. The older dial-out shape stays
reachable for rollback; see `docs/outbound-link.md` and `docs/architecture.md`.

Protocol derived from the satellite's own source (`reference/satellite.py`), whose
`WakeStreamingSatellite` class at line 1198 is the state machine we must match. The device
runs `--wake-uri tcp://127.0.0.1:10400 --wake-word-name hey_hermes`, so it uses that class
(not `AlwaysStreamingSatellite`, not `VadStreamingSatellite`).

Note on the wake URI: port 10400 is the wake **shim** (`pi/wake_shim.py`), not the wake
engine. The shim holds that port and forwards to `wyoming-openwakeword` on 10401, which is
what lets the Gateway open the microphone on demand for follow-up mode.

Handshake, exactly as the satellite implements it:

    us  -> satellite   RunSatellite            satellite unpauses and waits for its wake word
    sat -> us          Detection               the local wake word fired (hey_hermes)
    sat -> us          RunPipeline             start_stage=ASR end_stage=TTS snd_format=22050/2/1
    sat -> us          AudioChunk *            raw mic PCM: 16 kHz, S16_LE, mono
    us  -> satellite   Transcript              ends the utterance; satellite plays done-wav and
                                               returns to wake-word listening
    us  -> satellite   AudioStart/Chunk/Stop   handed straight to aplay: 22050 Hz, S16_LE, mono
    sat -> us          Played                  playback finished

Two facts that shape this module:

* AudioStart/AudioChunk/AudioStop are forwarded to the satellite's snd service
  UNCONDITIONALLY (SatelliteBase.event_from_server, lines 271-282), so playback can be
  proven with no wake word at all.
* Mic chunks are only forwarded while `is_streaming`, which the local wake word sets. A
  capture therefore requires a real wake event; there is no protocol way to ask for mic
  audio on demand.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from wyoming.asr import Transcript
from wyoming.audio import (
    AudioChunk,
    AudioChunkConverter,
    AudioStart,
    AudioStop,
    wav_to_chunks,
)
from wyoming.client import AsyncTcpClient
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.ping import Ping, Pong
from wyoming.satellite import (
    PauseSatellite,
    RunSatellite,
    StreamingStarted,
    StreamingStopped,
)
from wyoming.snd import Played
from wyoming.wake import Detection

from satellite_gateway.net import enable_tcp_keepalive

from satellite_gateway.config import SND_FORMAT

_LOGGER = logging.getLogger("satellite_gateway.satellite")

# Sentinel pushed on the queue when the connection ends, so consumers never hang.
_EOF = object()


class _suppress_cancel:
    """Context manager that swallows CancelledError."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, asyncio.CancelledError)


@dataclass
class CaptureResult:
    """Outcome of one mic capture."""

    path: Path
    seconds: float
    chunks: int
    rate: int
    width: int
    channels: int
    peak: int            # loudest absolute sample, 0-32767
    rms: float           # RMS across the whole capture
    detected: Optional[str] = None
    saw_run_pipeline: bool = False

    @property
    def dbfs(self) -> float:
        """Peak level in dBFS; -inf when silent."""
        return 20.0 * math.log10(self.peak / 32768.0) if self.peak > 0 else float("-inf")


class SatelliteClient:
    """Async Wyoming client for one satellite connection."""

    def __init__(self, host: str, port: int = 10700, *, log_events: bool = True) -> None:
        self.host = host
        self.port = port
        self._log_events = log_events
        self._client = AsyncTcpClient(host, port, connect_timeout=10.0)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        self._event_counts: dict[str, int] = {}
        self._eof = False

    # -- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        await self._client.connect()
        self._enable_tcp_keepalive()
        self._reader_task = asyncio.create_task(self._reader(), name="satellite-reader")
        _LOGGER.info("connected to wyoming satellite %s:%s", self.host, self.port)

    def _enable_tcp_keepalive(self) -> None:
        """Let the kernel notice a dead satellite, which the protocol cannot.

        An idle TCP connection has no timeout of its own, and the satellite says nothing while it
        waits for a wake word. Measured 2026-09-22: with the Pi off the network, the Gateway held a
        socket that still looked ESTAB and reported itself healthy with zero restarts, so a power
        cut left the satellite permanently inert. The satellite does NOT answer application-level
        pings (tested: a Ping draws no Pong), so keepalive is the only cheap liveness signal.

        With this, a peer that has gone away produces a socket error in about 30 seconds, the
        reader reports EOF, `wait_for_wake` raises ConnectionError, and systemd restarts us into a
        fresh connection.

        The mechanism and the values live in `gateway/net.py` now, because the accepted registry
        sockets need exactly the same thing and two copies of "15, 5, 3" would drift apart.
        """
        writer = getattr(self._client, "_writer", None)
        sock = writer.get_extra_info("socket") if writer is not None else None
        enable_tcp_keepalive(sock, label="dialled satellite", logger=_LOGGER)

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with _suppress_cancel():
                await self._reader_task
            self._reader_task = None
        with _suppress_cancel():
            await self._client.disconnect()
        _LOGGER.info("disconnected from %s:%s", self.host, self.port)

    async def reconnect(self) -> None:
        """Reopen the socket without restarting the process or reloading the models.

        2026-09-23. The Gateway used to exit on the hourly wake-word timeout, so systemd
        restarted it: the connect tone played every hour, whisper and Piper reloaded (about 640 MB
        churn), and the device was deaf for roughly nine seconds. Refreshing in place does the same
        job with none of that.

        `_eof` must be cleared and the stale queue drained, or the fresh link reports itself dead
        immediately and the caller loops. A brand new client is used rather than reusing the old
        one, so no half-closed state can survive the swap.
        """
        await self.close()
        self._client = AsyncTcpClient(self.host, self.port, connect_timeout=10.0)
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # `_eof` is cleared only once the new connection is actually up. It used to be cleared
        # before the connect, so a failed reconnect left the client reporting itself healthy (eof
        # False) with no reader task at all: the loop's `sat.eof` checks never fired, nothing
        # retried, and the device stayed deaf until the next hourly refresh (issue #16).
        #
        # On failure the link is deliberately left dead. The loop checks `sat.eof` when it waits for
        # a wake word, so it raises ConnectionError, and systemd restarts us into a fresh connection
        # in seconds rather than leaving an inert device for up to an hour. That is the same
        # recovery path `_enable_tcp_keepalive` documents for a peer that has gone away.
        try:
            await self.connect()
        except BaseException as exc:
            self._eof = True
            _LOGGER.warning("reconnect to %s:%s failed (%s: %s); leaving the link marked dead so the "
                            "loop notices instead of waiting for the next hourly refresh",
                            self.host, self.port, type(exc).__name__, exc)
            raise
        self._eof = False

    async def __aenter__(self) -> "SatelliteClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # -- event plumbing -----------------------------------------------------

    async def _reader(self) -> None:
        """Read events forever, answer pings, and hand the rest to the queue."""
        try:
            while True:
                event = await self._client.read_event()
                if event is None:
                    _LOGGER.info("satellite closed the connection")
                    self._queue.put_nowait(_EOF)
                    return
                self._event_counts[event.type] = self._event_counts.get(event.type, 0) + 1
                if Ping.is_type(event.type):
                    # The satellite expects a pong; it also enables its own ping once we ping it.
                    text = Ping.from_event(event).text
                    await self._client.write_event(Pong(text=text).event())
                    continue
                if self._log_events and not AudioChunk.is_type(event.type):
                    # Audio chunks are counted, never logged one-per-line: one utterance is
                    # ~130 of them and they bury everything else.
                    _LOGGER.info("sat <- us : %s", event.type)
                self._queue.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface it, never crash the loop
            _LOGGER.warning("reader stopped: %s: %s", type(exc).__name__, exc)
            self._queue.put_nowait(_EOF)

    async def send(self, event: Event) -> None:
        if self._log_events and not AudioChunk.is_type(event.type):
            _LOGGER.info("sat -> us : %s", event.type)
        await self._client.write_event(event)

    async def next_event(self, timeout: float) -> Optional[Event]:
        """Next event, or None on timeout. Distinguish a closed link with `.eof`."""
        try:
            if timeout <= 0:
                event = self._queue.get_nowait()
            else:
                event = await asyncio.wait_for(self._queue.get(), timeout)
        except (asyncio.QueueEmpty, asyncio.TimeoutError):
            return None
        if event is _EOF:
            self._eof = True
            return None
        return event

    # -- commands -----------------------------------------------------------

    async def describe(self) -> Optional[Info]:
        """Ask the satellite who it is. Returns None if it does not answer."""
        await self.send(Describe().event())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0 or self._eof:
                _LOGGER.warning("no info from the satellite")
                return None
            event = await self.next_event(remaining)
            if event is None:
                continue
            if Info.is_type(event.type):
                return Info.from_event(event)

    async def run(self) -> None:
        """Tell the satellite we are ready; it starts waiting for its wake word."""
        await self.send(RunSatellite().event())

    async def pause(self) -> None:
        await self.send(PauseSatellite().event())

    def out_format(self) -> tuple[int, int, int]:
        """The rate/width/channels to SEND this device.

        Decided by the DEVICE, not by the gateway. A fleet is one gateway plus any number of
        satellites in any mix of hardware, so the Gateway must never assume a particular host (or
        that a Raspberry Pi is present anywhere). Each device reports the rate its own audio path
        runs at (`snd_rate`); that is what it gets sent, and 22050 stays the default for anything
        that has not said otherwise.

        An ESP32 satellite reports 48000 because its I2S link is a 48 kHz master, and then it
        needs to do no conversion at all -- the sample rate costs it nothing on a path where its
        own CPU is the scarce resource. A Pi satellite reports 22050 and keeps receiving Piper's
        native rate.
        """
        rate = getattr(self, "snd_rate", None)
        if isinstance(rate, int) and rate > 0:
            return (rate, SND_FORMAT[1], SND_FORMAT[2])
        return SND_FORMAT

    def tts_stream(self, *, pace: float = 2.0):
        """Open a streaming TTS session: `async with sat.tts_stream() as s: await s.say(text)`.

        Sends audio-start on entry and audio-stop on exit, so an answer is one playback
        session rather than one per fragment, and the satellite's tts-start/stop events fire
        once per answer.

        Fleet (issue #111): the stream takes this client's playback lock. A dialled client
        shares the process-wide class lock exactly as before; a satellite lease overrides it
        with its own, so two devices' answers never serialize behind each other.
        """
        from satellite_gateway.tts import TtsStream

        return TtsStream(self.send, pace=pace,
                         lock=getattr(self, "_playback_lock_override", None),
                         fmt=self.out_format())

    # -- playback -----------------------------------------------------------

    # One playback at a time across every client in this process. The satellite has a single
    # snd service (one aplay process), so two concurrent streams would interleave into it, and
    # the announcement path deliberately uses its own connection, so a per-instance lock would
    # not be enough.
    #
    # Fleet exception (issue #111): the "one aplay" argument is per DEVICE, not per process.
    # Two devices have two aploys; a LeasedSatellite sets `_playback_lock_override` to its own
    # lock, and every playback entry point here resolves through `playback_lock` so a lease
    # serializes against its own announcements and streams, and never against another device.
    PLAYBACK_LOCK = asyncio.Lock()

    @property
    def playback_lock(self) -> asyncio.Lock:
        """This client's playback lock, resolved late so tests that swap the class lock still
        affect clients that use it."""
        return getattr(self, "_playback_lock_override", None) or SatelliteClient.PLAYBACK_LOCK

    async def play_wav(self, *args, **kwargs) -> bool:
        """Serialised entry point; the real work is in `_play_wav_inner`."""
        async with self.playback_lock:
            return await self._play_wav_inner(*args, **kwargs)

    async def _play_wav_inner(self, wav_path: Path, *, wait_for_played: bool = True,
                       timeout: float = 180.0, samples_per_chunk: int = 1024,
                       convert_to_snd_format: bool = True, pace: float = 2.0) -> bool:
        """Stream a WAV to the satellite's speaker. True when it reports Played.

        Resampling happens here, not on the Pi. The satellite does convert correctly
        (wyoming's `SndProcessAsyncClient` holds an `AudioChunkConverter` built from its own
        snd format, so any input rate arrives right), but doing it on this host keeps the
        Pi's CPU for the wake word and makes our output format explicit.

        `pace` sends at N x realtime; 0 disables pacing. Without it the entire clip is
        pushed in milliseconds and aplay buffers all of it, which is unbounded for long
        responses on a 416 MB board.
        """
        with wave.open(str(wav_path), "rb") as wf:
            src = (wf.getframerate(), wf.getsampwidth(), wf.getnchannels())
            out = self.out_format() if convert_to_snd_format else src
            converter = AudioChunkConverter(*out)
            # The device is named here because without it a two-device fleet is indistinguishable
            # from one device being sent the same clip twice -- and the difference matters: a
            # duplicate on one speaker is a bug, one clip per speaker is correct behaviour.
            _LOGGER.info("playing %s (%s Hz/%sch -> %s Hz/%sch, %.2f s) on %s",
                         wav_path.name, src[0], src[2], out[0], out[2],
                         wf.getnframes() / max(src[0], 1),
                         getattr(self, "device_id", None) or "unknown-device")
            # The satellite ignores audio-start/stop for playback (only AudioChunk reaches
            # aplay) but it uses them for its tts-start/stop events, so send them properly.
            await self.send(AudioStart(rate=out[0], width=out[1], channels=out[2]).event())
            for event in wav_to_chunks(wf, samples_per_chunk=samples_per_chunk):
                chunk = converter.convert(event)
                await self.send(chunk.event())
                if pace:
                    await asyncio.sleep(chunk.seconds / pace)
            await self.send(AudioStop().event())
            if not wait_for_played:
                # Sending is paced at N x realtime, so roughly (1 - 1/N) of the clip is still
                # queued in the satellite's aplay buffer when the send returns. Sleep that out
                # while the caller still holds the playback lock, so nothing else starts speaking
                # on top of this clip. A caller sharing another component's connection cannot
                # instead wait for `Played`, because that component's capture would swallow it.
                total = wf.getnframes() / max(src[0], 1)
                playout = total * (1.0 - 1.0 / pace) if pace else total
                if playout > 0:
                    await asyncio.sleep(playout + 0.3)
                return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                _LOGGER.warning("timed out waiting for Played")
                return False
            event = await self.next_event(remaining)
            if event is None:
                if self._eof:
                    _LOGGER.warning("connection closed before Played arrived")
                    return False
                continue
            if Played.is_type(event.type):
                _LOGGER.info("satellite reports playback finished")
                return True

    # -- capture ------------------------------------------------------------

    async def capture(self, out_path: Path, *, seconds: float = 5.0,
                      max_wait: float = 120.0, require_wake: bool = True,
                      stop_with_transcript: bool = True) -> CaptureResult:
        """Capture `seconds` of mic audio to a WAV.

        With `require_wake` (the default) this waits for the satellite's local wake word,
        because mic chunks are only forwarded while it is streaming. The 16 kHz S16 mono
        format is read from the chunks themselves, never assumed.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait
        buffers: list[bytes] = []
        rate = width = channels = None
        detected: Optional[str] = None
        saw_run_pipeline = False
        captured_samples = 0

        _LOGGER.info("waiting up to %.0fs for %s", max_wait,
                     "the wake word" if require_wake else "audio")
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"no audio within {max_wait:.0f}s"
                    + (" (the wake word never fired)" if require_wake else "")
                )
            event = await self.next_event(remaining)
            if event is None:
                if self._eof:
                    raise ConnectionError("satellite connection closed during capture")
                continue
            if event.type == "run-pipeline":
                saw_run_pipeline = True
                continue
            if Detection.is_type(event.type):
                detected = Detection.from_event(event).name
                _LOGGER.info("wake word detected: %s", detected)
                continue
            if StreamingStarted.is_type(event.type):
                _LOGGER.info("satellite started streaming mic audio")
                continue
            if StreamingStopped.is_type(event.type):
                _LOGGER.info("satellite stopped streaming")
                continue
            if AudioChunk.is_type(event.type):
                chunk = AudioChunk.from_event(event)
                if rate is None:
                    rate, width, channels = chunk.rate, chunk.width, chunk.channels
                    _LOGGER.info("mic format from the stream: %s Hz, %s bytes, %s ch",
                                 rate, width, channels)
                buffers.append(chunk.audio)
                captured_samples += chunk.samples
                if rate and captured_samples >= seconds * rate:
                    break

        if not buffers or rate is None:
            raise RuntimeError("no audio chunks arrived")

        audio = b"".join(buffers)
        if stop_with_transcript:
            # The satellite treats Transcript as "utterance over": it stops streaming and
            # returns to wake-word listening. This is the protocol's own stop signal.
            await self.send(Transcript(text="").event())

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(width)
            wf.setframerate(rate)
            wf.writeframes(audio)

        import numpy as np

        samples = np.frombuffer(audio, dtype="<i2")
        peak = int(np.abs(samples).max()) if samples.size else 0
        rms = float(np.sqrt(np.mean(samples.astype("f8") ** 2))) if samples.size else 0.0
        result = CaptureResult(
            path=out_path, seconds=len(audio) / (width * channels * rate), chunks=len(buffers),
            rate=rate, width=width, channels=channels, peak=peak, rms=rms,
            detected=detected, saw_run_pipeline=saw_run_pipeline,
        )
        _LOGGER.info("captured %.2f s (%d chunks) peak=%d rms=%.1f", result.seconds,
                     result.chunks, result.peak, result.rms)
        return result

    # -- introspection ------------------------------------------------------

    @property
    def eof(self) -> bool:
        """True once the satellite has closed the connection."""
        return self._eof

    @property
    def event_counts(self) -> dict[str, int]:
        return dict(self._event_counts)
