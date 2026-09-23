"""VAD endpointing: turn a continuous mic stream into exactly one trimmed utterance.

This is the piece that stops the assistant recording dead air. Phase 2 captured a fixed 5
seconds, which meant 2.4 s of leading silence in one test and a 5 s window regardless of
how briefly Angela spoke.

Design notes that come straight from Phase 2's failures:

* **Pre-roll buffer.** A VAD needs a few frames of speech before it fires, so a naive
  "start recording when the VAD says speech" loses the first 100-200 ms of the word. We
  already fixed exactly that class of bug on the satellite side (the mic mute was clipping
  the onset of Angela's first word), so repeating it here would be careless. The last
  `pre_roll_ms` of non-speech frames are prepended when speech starts.
* **Frame quantisation.** webrtcvad only accepts 10, 20 or 30 ms frames of 16-bit mono at
  8/16/32/48 kHz, so the buffer is sliced to that size and any partial frame is held over.
* **Measured VAD choice.** On Angela's real capture webrtcvad at aggressiveness 2 put its
  first speech frame at 0.54 s against a measured truth of 0.537 s, within one frame. It is
  tiny and cheap on CPU. `pysilero_vad` is a viable higher-quality swap (it exports
  `SileroVoiceActivityDetector` and ships a ggml model) if the room gets noisier.

Two offsets are reported, because conflating them caused a bad test once:

* `first_speech_at`   - where speech begins INSIDE the returned clip (bounded by pre_roll_ms)
* `speech_absolute_at` - where speech begins in the ORIGINAL stream, measured from the first
  byte ever fed in. Compare this one against external ground truth.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass
from typing import Deque, Optional

import webrtcvad

_LOGGER = logging.getLogger("bridge.endpointing")

_DEFAULT_RATE = 16000
_VALID_FRAME_MS = (10, 20, 30)


@dataclass
class EndpointResult:
    """One detected utterance."""

    audio: bytes
    rate: int
    duration: float
    speech_seconds: float
    first_speech_at: float
    speech_absolute_at: float
    ended_by: str          # "silence" | "max_length" | "no_speech"
    rejected: bool = False  # too little speech to be worth transcribing

    @property
    def bytes_per_second(self) -> int:
        return self.rate * 2  # 16-bit mono

    def __str__(self) -> str:
        return (f"{self.duration:.2f}s audio ({len(self.audio)} bytes), "
                f"{self.speech_seconds:.2f}s speech, "
                f"speech at {self.first_speech_at:.2f}s in clip "
                f"/ {self.speech_absolute_at:.2f}s in stream, "
                f"ended_by={self.ended_by}"
                + (", REJECTED" if self.rejected else ""))


class Endpointer:
    """Streaming VAD endpointer. Feed it audio, get back one utterance at a time."""

    def __init__(self, *, rate: int = _DEFAULT_RATE, frame_ms: int = 20,
                 vad_aggressiveness: int = 2, silence_ms: int = 700,
                 min_speech_ms: int = 250, max_utterance_seconds: float = 15.0,
                 pre_roll_ms: int = 300, tail_ms: int = 200,
                 no_speech_timeout_seconds: float = 8.0) -> None:
        if frame_ms not in _VALID_FRAME_MS:
            raise ValueError(f"frame_ms must be one of {_VALID_FRAME_MS}")
        if not 0 <= vad_aggressiveness <= 3:
            raise ValueError("vad_aggressiveness must be 0-3")

        self.rate = rate
        self.frame_ms = frame_ms
        self.frame_samples = rate * frame_ms // 1000
        self.frame_bytes = self.frame_samples * 2

        self._vad = webrtcvad.Vad(vad_aggressiveness)
        self._silence_frames = max(1, silence_ms // frame_ms)
        self._min_speech_frames = max(1, min_speech_ms // frame_ms)
        self._max_frames = max(1, int(max_utterance_seconds * 1000 / frame_ms))
        self._tail_frames = max(0, tail_ms // frame_ms)
        self._pre_roll_frames = max(1, pre_roll_ms // frame_ms)
        self._no_speech_frames = max(1, int(no_speech_timeout_seconds * 1000 / frame_ms))

        self.reset()

    # -- state ---------------------------------------------------------------

    def reset(self) -> None:
        self._buffer = bytearray()
        self._pre_roll: Deque[bytes] = collections.deque(maxlen=self._pre_roll_frames)
        self._frames: list[bytes] = []
        self._in_speech = False
        self._speech_frames = 0
        self._trailing_silence = 0
        self._first_speech_frame: Optional[int] = None
        self._speech_started_at_frame: Optional[int] = None
        self._frames_seen = 0

    # -- streaming ----------------------------------------------------------

    def process(self, audio: bytes) -> Optional[EndpointResult]:
        """Feed raw 16-bit mono PCM. Returns a result once an utterance completes."""
        self._buffer.extend(audio)
        while len(self._buffer) >= self.frame_bytes:
            frame = bytes(self._buffer[: self.frame_bytes])
            del self._buffer[: self.frame_bytes]
            result = self._process_frame(frame)
            if result is not None:
                return result
        return None

    def _process_frame(self, frame: bytes) -> Optional[EndpointResult]:
        self._frames_seen += 1
        is_speech = self._vad.is_speech(frame, self.rate)

        if not self._in_speech:
            if is_speech:
                self._in_speech = True
                # Prepend the pre-roll so the onset of the word survives.
                self._frames = list(self._pre_roll)
                self._pre_roll.clear()
                self._first_speech_frame = len(self._frames)
                self._speech_started_at_frame = self._frames_seen
                self._frames.append(frame)
                self._speech_frames = 1
                self._trailing_silence = 0
            else:
                self._pre_roll.append(frame)
                if self._frames_seen >= self._no_speech_frames:
                    return self._finish("no_speech")
            return None

        self._frames.append(frame)
        if is_speech:
            self._speech_frames += 1
            self._trailing_silence = 0
        else:
            self._trailing_silence += 1

        if self._trailing_silence >= self._silence_frames:
            return self._finish("silence")
        if len(self._frames) >= self._max_frames:
            return self._finish("max_length")
        return None

    def _finish(self, reason: str) -> EndpointResult:
        frames = self._frames
        if reason == "silence":
            # Drop most of the trailing silence but keep a short natural tail.
            keep = len(frames) - self._trailing_silence + self._tail_frames
            frames = frames[: max(keep, 1)]

        audio = b"".join(frames)
        result = EndpointResult(
            audio=audio,
            rate=self.rate,
            duration=len(audio) / (self.rate * 2),  # 16-bit mono: 2 bytes per sample
            speech_seconds=self._speech_frames * self.frame_ms / 1000,
            first_speech_at=(self._first_speech_frame or 0) * self.frame_ms / 1000,
            speech_absolute_at=((self._speech_started_at_frame or 1) - 1) * self.frame_ms / 1000,
            ended_by=reason,
            rejected=reason != "max_length" and self._speech_frames < self._min_speech_frames,
        )
        _LOGGER.info("endpoint (%s): %s", reason, result)
        self.reset()
        return result
