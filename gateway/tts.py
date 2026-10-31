"""Piper text-to-speech for the gateway.

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
from piper.config import SynthesisConfig

from gateway.config import (
    PIPER_LENGTH_SCALE,
    PIPER_SPEAKER_SETTING,
    SND_FORMAT,
    VOICE_CONFIG,
    VOICE_MODEL,
)
# The speaker rules live in gateway/voices.py so scripts/switch-voice.py can apply exactly the
# same resolution before it writes any configuration, without importing onnxruntime. Re-exported
# here because this module is where callers (and tests) expect to find it.
from gateway.voices import resolve_speaker_id
import asyncio

_LOGGER = logging.getLogger("gateway.tts")

# The voice config path resolves against the configured voice name. Module-level so tests can
# point it at a fixture without a real .onnx.
PIPER_VOICE_MODEL_CONFIG_PATH = VOICE_CONFIG


def build_synthesis_config():
    """The Piper SynthesisConfig for every synthesis this process does.

    Built per call on purpose: it is cheap, and resolving the speaker per call means a bad
    setting surfaces as a clear ValueError in the log at the first synthesis, not as a wrong
    voice. load_voice()'s cache keeps the expensive part (the model) warm.
    """
    syn = SynthesisConfig()
    syn.speaker_id = resolve_speaker_id(PIPER_SPEAKER_SETTING, PIPER_VOICE_MODEL_CONFIG_PATH)
    if PIPER_LENGTH_SCALE is not None:
        syn.length_scale = PIPER_LENGTH_SCALE
    return syn


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
        voice.synthesize_wav(text, wav_file, syn_config=build_synthesis_config())
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
    for chunk in voice.synthesize(text, syn_config=build_synthesis_config()):
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

    def __init__(self, send, *, pace: float = 2.0, lock=None,
                 fmt: tuple[int, int, int] | None = None) -> None:
        from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop

        self._AudioChunk = AudioChunk
        self._AudioStart = AudioStart
        self._AudioStop = AudioStop
        # What format to send. Default historic 22050 so nothing changes for the Pi satellite;
        # the board's 48 kHz mode is opted into per device (see SatelliteClient.out_format).
        self._fmt = fmt or SND_FORMAT
        self._converter = AudioChunkConverter(*self._fmt)
        self._send = send
        self._pace = pace
        self._seconds_sent = 0.0
        self._chars_sent = 0
        self._holds_lock = False
        # Per-device playback (issue #111): a satellite lease passes its own device lock so
        # two devices' speech never serializes behind each other. None means the process-wide
        # class lock, resolved late in __aenter__ — the dialled path and the lock tests swap
        # `SatelliteClient.PLAYBACK_LOCK` between cases, and late resolution is what keeps
        # them seeing the current lock rather than a captured one.
        self._lock = lock
        self._acquired_lock = None

    async def __aenter__(self) -> "TtsStream":
        # Hold the process-wide playback lock for the whole stream. The announcement path uses
        # its own connection to the satellite, so without this an announcement and a streamed
        # answer could interleave into the satellite's single aplay process.
        #
        # Imported in BOTH this method and __aexit__ on purpose. A name imported inside one method
        # is not visible in another: getting that wrong once meant the release raised NameError
        # inside its own finally, so the lock stayed held forever and every later answer blocked.
        # The device simply looked stuck.
        from gateway.satellite import SatelliteClient

        lock = self._lock if self._lock is not None else SatelliteClient.PLAYBACK_LOCK
        try:
            await asyncio.wait_for(lock.acquire(), timeout=30.0)
        except asyncio.TimeoutError as exc:
            # Fail loudly rather than hanging in silence: a wedged lock should appear in the log
            # as this message, not as a device that stops answering.
            raise RuntimeError(
                "could not acquire the playback lock within 30s; something is holding it"
            ) from exc
        self._holds_lock = True
        self._acquired_lock = lock
        try:
            await self._send(self._AudioStart(rate=self._fmt[0], width=self._fmt[1],
                                              channels=self._fmt[2]).event())
        except BaseException:
            # `__aexit__` is NOT called when `__aenter__` raises, so the lock has to be handed back
            # here. Without this a failed audio-start (satellite connection gone, or a cancellation
            # mid-entry) held the process-wide playback lock forever: every later answer waited 30s
            # and then failed, and the device looked simply stuck. Same failure class the note above
            # records, on the path that note did not cover (issue #14). BaseException on purpose, so
            # a CancelledError releases it too.
            lock.release()
            self._holds_lock = False
            self._acquired_lock = None
            raise
        return self

    async def __aexit__(self, *exc) -> bool:
        from gateway.satellite import SatelliteClient

        try:
            await self._send(self._AudioStop().event())
            _LOGGER.info("tts stream closed: %d chars, %.2f s of audio", self._chars_sent,
                         self._seconds_sent)
        except (Exception, asyncio.CancelledError):
            # Cleanup must not replace the failure (or cancellation) that led here.
            # A stop error on an otherwise successful stream must still propagate.
            if exc[1] is None:
                raise
            _LOGGER.warning("tts: audio-stop failed while handling %s; preserving original error",
                            type(exc[1]).__name__, exc_info=True)
        finally:
            if self._holds_lock:
                self._holds_lock = False
                # Release exactly the lock this stream acquired, whatever it turned out to be.
                (self._acquired_lock or SatelliteClient.PLAYBACK_LOCK).release()
                self._acquired_lock = None
        return False

    async def say(self, text: str) -> float:
        """Synthesize `text` and stream it. Returns the seconds of audio sent."""
        import asyncio

        text = text.strip()
        if not text:
            return 0.0
        sent = 0.0
        chunks_sent = 0
        _LOGGER.info("tts: synthesis started (%d chars)", len(text))
        for pcm, rate, width, channels in synthesize_chunks(text):
            converted = self._converter.convert(
                self._AudioChunk(rate=rate, width=width, channels=channels, audio=pcm))
            # Piper yields whole sentences, not bounded transport frames. Embedded peers
            # cap payloads at 16 KiB; use smaller, sample-aligned chunks and pace each one.
            frame_bytes = converted.width * converted.channels
            chunk_bytes = (4096 // frame_bytes) * frame_bytes
            if chunk_bytes == 0 or len(converted.audio) % frame_bytes:
                raise ValueError("TTS output is not sample-aligned")
            for offset in range(0, len(converted.audio), chunk_bytes):
                chunk = self._AudioChunk(rate=converted.rate, width=converted.width,
                                         channels=converted.channels,
                                         audio=converted.audio[offset:offset + chunk_bytes])
                if chunks_sent == 0:
                    _LOGGER.info("tts: sending first PCM chunk (%d bytes, %d Hz/%d/%d)",
                                 len(chunk.audio), chunk.rate, chunk.width, chunk.channels)
                await self._send(chunk.event())
                chunks_sent += 1
                sent += chunk.seconds
                self._seconds_sent += chunk.seconds
                if self._pace:
                    await asyncio.sleep(chunk.seconds / self._pace)
        _LOGGER.info("tts: transmitted %d chunks, %.2fs of audio (not playback confirmation)",
                     chunks_sent, sent)
        self._chars_sent += len(text)
        return sent

    @property
    def seconds_sent(self) -> float:
        return self._seconds_sent
