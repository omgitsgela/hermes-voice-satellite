"""Piper text-to-speech for the bridge.

Piper medium voices are natively 22050 Hz, 16-bit, mono, which is exactly what the
satellite's `aplay -r 22050 -c 1 -f S16_LE` expects, so no resampling is needed.

Phase 2 writes a complete WAV and streams it; Phase 3 will consume `synthesize()`'s
chunk generator directly so playback can start before the sentence finishes generating.
"""

from __future__ import annotations

import logging
import wave
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from piper.voice import PiperVoice

from bridge.config import SND_FORMAT, VOICE_CONFIG, VOICE_MODEL
import asyncio

_LOGGER = logging.getLogger("bridge.tts")


@lru_cache(maxsize=2)
def load_voice(model_path: str = str(VOICE_MODEL),
               config_path: str = str(VOICE_CONFIG)) -> PiperVoice:
    """Load (and cache) the Piper voice. Model loading is the slow part, so keep it warm."""
    _LOGGER.info("loading Piper voice %s", Path(model_path).name)
    return PiperVoice.load(model_path, config_path=config_path)


def synthesize_to_wav(text: str, out_path: Path) -> Path:
    """Synthesize `text` to a WAV file and return its path."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    voice = load_voice()
    with wave.open(str(out_path), "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)
    _LOGGER.info("synthesized %d chars -> %s", len(text), out_path.name)
    return out_path


def describe_wav(path: Path) -> dict:
    """Read back a WAV's real format, so callers verify rather than assume."""
    with wave.open(str(path), "rb") as wf:
        return {
            "path": str(path),
            "rate": wf.getframerate(),
            "width": wf.getsampwidth(),
            "channels": wf.getnchannels(),
            "frames": wf.getnframes(),
            "seconds": wf.getnframes() / max(wf.getframerate(), 1),
        }


def synthesize_chunks(text: str) -> Iterator[tuple[bytes, int, int, int]]:
    """Yield (pcm, rate, width, channels) as Piper produces it.

    Streaming instead of writing a WAV first is what lets the satellite start speaking
    before the whole sentence has been synthesized.
    """
    voice = load_voice()
    for chunk in voice.synthesize(text):
        pcm = chunk.audio_int16_bytes
        if pcm:
            yield pcm, chunk.sample_rate, chunk.sample_width, chunk.sample_channels


class TtsStream:
    """Send synthesized speech to the satellite as it is produced.

    Opens with `audio-start` and closes with `audio-stop`, so the satellite's tts-start and
    tts-stop events fire exactly once per answer and aplay is started and torn down once
    rather than per fragment. `send` is the satellite client's `send` coroutine, injected so
    this stays independent of it.

    `pace` sends at N x realtime. The satellite converts our 22050 Hz output to its own snd
    format itself, but pacing still matters: without it Piper's output is pushed far faster
    than realtime and aplay buffers the whole answer in a 416 MB board's memory.
    """

    def __init__(self, send, *, pace: float = 2.0) -> None:
        from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop

        self._AudioChunk = AudioChunk
        self._AudioStart = AudioStart
        self._AudioStop = AudioStop
        self._converter = AudioChunkConverter(*SND_FORMAT)
        self._send = send
        self._pace = pace
        self._seconds_sent = 0.0
        self._chars_sent = 0
        self._holds_lock = False

    async def __aenter__(self) -> "TtsStream":
        # Hold the process-wide playback lock for the whole stream. The announcement path uses
        # its own connection to the satellite, so without this an announcement and a streamed
        # answer could interleave into the satellite's single aplay process.
        #
        # Imported in BOTH this method and __aexit__ on purpose. A name imported inside one method
        # is not visible in another: getting that wrong once meant the release raised NameError
        # inside its own finally, so the lock stayed held forever and every later answer blocked.
        # The device simply looked stuck.
        from bridge.satellite import SatelliteClient

        try:
            await asyncio.wait_for(SatelliteClient.PLAYBACK_LOCK.acquire(), timeout=30.0)
        except asyncio.TimeoutError as exc:
            # Fail loudly rather than hanging in silence: a wedged lock should appear in the log
            # as this message, not as a device that stops answering.
            raise RuntimeError(
                "could not acquire the playback lock within 30s; something is holding it"
            ) from exc
        self._holds_lock = True
        try:
            await self._send(self._AudioStart(rate=SND_FORMAT[0], width=SND_FORMAT[1],
                                              channels=SND_FORMAT[2]).event())
        except BaseException:
            # `__aexit__` is NOT called when `__aenter__` raises, so the lock has to be handed back
            # here. Without this a failed audio-start (satellite connection gone, or a cancellation
            # mid-entry) held the process-wide playback lock forever: every later answer waited 30s
            # and then failed, and the device looked simply stuck. Same failure class the note above
            # records, on the path that note did not cover (issue #14). BaseException on purpose, so
            # a CancelledError releases it too.
            SatelliteClient.PLAYBACK_LOCK.release()
            self._holds_lock = False
            raise
        return self

    async def __aexit__(self, *exc) -> bool:
        from bridge.satellite import SatelliteClient

        try:
            await self._send(self._AudioStop().event())
            _LOGGER.info("tts stream closed: %d chars, %.2f s of audio", self._chars_sent,
                         self._seconds_sent)
        finally:
            if self._holds_lock:
                self._holds_lock = False
                SatelliteClient.PLAYBACK_LOCK.release()
        return False

    async def say(self, text: str) -> float:
        """Synthesize `text` and stream it. Returns the seconds of audio sent."""
        import asyncio

        text = text.strip()
        if not text:
            return 0.0
        sent = 0.0
        for pcm, rate, width, channels in synthesize_chunks(text):
            converted = self._converter.convert(
                self._AudioChunk(rate=rate, width=width, channels=channels, audio=pcm))
            await self._send(converted.event())
            sent += converted.seconds
            if self._pace:
                await asyncio.sleep(converted.seconds / self._pace)
        self._seconds_sent += sent
        self._chars_sent += len(text)
        return sent

    @property
    def seconds_sent(self) -> float:
        return self._seconds_sent
