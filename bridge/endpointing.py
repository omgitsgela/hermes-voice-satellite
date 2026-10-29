"""VAD endpointing: turn a continuous mic stream into exactly one trimmed utterance.

This is the piece that stops the assistant recording dead air. Phase 2 captured a fixed 5
seconds, which meant 2.4 s of leading silence in one test and a 5 s window regardless of
how briefly the person spoke.

Design notes that come straight from Phase 2's failures:

* **Pre-roll buffer.** A VAD needs a few frames of speech before it fires, so a naive
  "start recording when the VAD says speech" loses the first 100-200 ms of the word. We
  already fixed exactly that class of bug on the satellite side (the mic mute was clipping
  the onset of the first word), so repeating it here would be careless. The last
  `pre_roll_ms` of non-speech frames are prepended when speech starts.
* **Frame quantisation.** webrtcvad only accepts 10, 20 or 30 ms frames of 16-bit mono at
  8/16/32/48 kHz, so the buffer is sliced to that size and any partial frame is held over.
* **Measured VAD choice.** On the real capture webrtcvad at aggressiveness 2 put its
  first speech frame at 0.54 s against a measured truth of 0.537 s, within one frame. It is
  tiny and cheap on CPU. `pysilero_vad` is a viable higher-quality swap (it exports
  `SileroVoiceActivityDetector` and ships a ggml model) if the room gets noisier.

Two offsets are reported, because conflating them caused a bad test once:

* `first_speech_at`   - where speech begins INSIDE the returned clip (bounded by pre_roll_ms)
* `speech_absolute_at` - where speech begins in the ORIGINAL stream, measured from the first
  byte ever fed in. Compare this one against external ground truth.
"""

from __future__ import annotations

import array
import collections
import logging
import math
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


class EnergyGate:
    """Reject frames too quiet to be a person, judged against the room's own floor.

    webrtcvad decides on spectral content, not energy, so a room's fan can produce frames it calls
    speech while sitting far below anyone's voice. Measured on the device, 2026-09-24: fan baseline
    median RMS 191 with a floor max of 422, a YouTube video at normal volume peaking at 871, and the
    maintainer talking at 1026 median rising to 9343. A fan blip was logged every few seconds.

    The floor is a low percentile of recent frames the detector did NOT call speech, so the estimate
    comes from the room rather than from the speaker, and a long monologue cannot drag it up: pauses
    keep feeding it. Until the window has `min_samples`, `floor_default` is used, which is the
    measured fan level for this hardware rather than a guess.

    This gate can only make the device less sensitive, never more.
    """

    def __init__(self, *, ratio: float = 6.0, floor_default: float = 190.0,
                 floor_min: float = 60.0, floor_max: float = 1000.0,
                 window_frames: int = 1500, min_samples: int = 40,
                 threshold_min: float = 400.0, threshold_max: float = 1000.0,
                 percentile: float = 0.20) -> None:
        self.ratio = ratio
        self.floor_default = floor_default
        self.floor_min = floor_min
        self.floor_max = floor_max
        self.min_samples = min_samples
        self.threshold_min = threshold_min
        self.threshold_max = threshold_max
        self.percentile = percentile
        self.floor = floor_default
        # Until enough non-speech frames have been seen, `floor` is only the DEFAULT, not a
        # measurement of this room. That distinction has to be tracked, because the default
        # multiplied by the ratio lands ABOVE the ceiling and pins the gate to it (see threshold).
        self._floor_measured = False
        self._window: Deque[float] = collections.deque(maxlen=window_frames)

    @property
    def threshold(self) -> float:
        """Speech must clear this.

        Floored so a dead-silent room cannot drive it to zero, and CEILINGED so a loud one cannot
        drive it above the person being listened for. The ceiling is the part that was missing, and
        its absence is not theoretical: `floor` and `ratio` are both properties of the ROOM, so a
        busy office whose floor learns ~540 produced a threshold of ~3240 and the device stopped
        hearing the person altogether, while the same person in a quiet room was heard fine. Being deaf
        to the person is the worse failure, so the person wins any disagreement with the room.

        `threshold_max` is an absolute level rather than a learned one on purpose. A learned
        speech level would be the natural pairing, but the estimate would have to come from frames
        the detector called speech, and the detector misses speech - measured 2026-09-25, raising
        the floor percentile to 0.85 let exactly those missed frames dominate the estimate and the
        gate clamped itself to its loudest cap. The low percentile plus a fixed ceiling keeps the
        room estimate robust and bounds the failure at a number a person can be measured against.
        """
        if not self._floor_measured:
            # The warmup case, and it was a real, reproducible failure rather than a tidy-up. Before
            # the floor is measured the gate used `floor_default` (190) * ratio (6) = 1140, which the
            # ceiling turns into 1000 -- the gate's absolute maximum -- for the first `min_samples`
            # frames of every stream (0.8 s). That is exactly the window in which a person says
            # "hello": measured 2026-10-29, speech at median RMS 628 was rejected 35 frames out of 37
            # and the turn died as "nothing said after the wake word", i.e. the assistant was deaf
            # at the moment it was addressed.
            #
            # A default is not a measurement, so it must not be able to raise the gate at all: before
            # the room is known, only the fixed floor applies.
            return self.threshold_min
        return min(self.threshold_max, max(self.threshold_min, self.floor * self.ratio))

    def observe(self, frame: bytes, detector_says_speech: bool) -> float:
        """Record one frame's level and return it. Only non-speech frames inform the floor."""
        level = frame_rms(frame)
        if not detector_says_speech:
            self._window.append(level)
            self._recompute_floor()
        return level

    def _recompute_floor(self) -> None:
        if len(self._window) < self.min_samples:
            return
        ordered = sorted(self._window)
        index = max(0, min(len(ordered) - 1, int(len(ordered) * self.percentile) - 1))
        self.floor = min(self.floor_max, max(self.floor_min, ordered[index]))
        self._floor_measured = True

    def passes_rms(self, level: float) -> bool:
        return level > self.threshold

    def passes(self, frame: bytes) -> bool:
        return self.passes_rms(frame_rms(frame))

    def stats(self) -> dict:
        """A snapshot of what this gate currently believes about the room.

        Added because the gate's decision was invisible. A turn could fail with "nothing said
        after the wake word" and nothing on the box recorded whether the detector or the gate
        had refused the frames, or what the room looked like when it happened. That gap is what
        made a real mis-calibration take a day to find instead of one turn.

        The percentiles matter as much as the threshold: the floor is a LOW percentile of
        non-speech frames, while the thing the gate exists to reject (a fan) is loud at its
        peaks. Seeing p20 next to max shows whether the estimate is tracking the room's quiet
        stretches or its actual noise.
        """
        window = sorted(self._window)

        def pct(p: float) -> float:
            if not window:
                return 0.0
            index = max(0, min(len(window) - 1, int(len(window) * p) - 1))
            return window[index]

        return {
            "floor": self.floor,
            "floor_measured": self._floor_measured,
            "threshold": self.threshold,
            "samples": len(window),
            "p20": pct(0.20),
            "p50": pct(0.50),
            "p90": pct(0.90),
            "max": window[-1] if window else 0.0,
        }


def frame_rms(frame: bytes) -> float:
    """Root-mean-square level of one 16-bit mono frame, in sample units."""
    usable = len(frame) - (len(frame) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(frame[:usable])
    total = 0
    for value in samples:
        total += value * value
    return math.sqrt(total / len(samples))


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

        # The detector is selectable (see VOICE_DETECTOR in config). Silero is the default because
        # webrtcvad calls a loud room "speech"; `webrtcvad` remains reachable so the two can be
        # compared by configuration rather than by argument.
        from bridge.config import SILERO_THRESHOLD, VOICE_DETECTOR
        from bridge.silero_vad import make_vad

        self._vad = make_vad(
            VOICE_DETECTOR, rate=rate, aggressiveness=vad_aggressiveness,
            threshold=SILERO_THRESHOLD,
        )
        # The energy gate sits alongside the detector rather than replacing it: the detector still
        # decides what looks like speech, and the gate decides whether it is loud enough to be a
        # person. Deliberately NOT reset by reset(), which runs after every utterance: the room's
        # floor is a property of the room, not of the utterance.
        from bridge.config import VOICE_ENERGY_GATE

        self.gate = EnergyGate(**VOICE_ENERGY_GATE)
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
        # Per-utterance diagnostics. The detector and the energy gate can each refuse a frame,
        # and the two failures need OPPOSITE fixes: a high `gate_rejected` means the level gate
        # is deaf to the speaker, a low `vad_speech` means the spectral detector is. Keeping
        # them apart is the whole point - a single "speech" count cannot tell them apart.
        self._vad_speech_frames = 0
        self._gate_rejected_frames = 0
        self._accepted_frames = 0
        self._speech_levels: list[float] = []
        # The detector is reset here too, not just the endpointer's own buffers. The Silero
        # detector (the default) carries state between utterances - a 512-sample buffer, LSTM h/c,
        # and the latched last-window probability - and a 20 ms frame never completes a window, so
        # the first frame of a fresh utterance would otherwise answer with the previous utterance's
        # decision. The guard is for the webrtcvad detector, which is stateless and has no reset():
        # resetting only what is stateful is exactly the reset the Silero detector documents
        # ("called between utterances"). The energy gate stays on purpose (the room's floor is the
        # room's, not the utterance's).
        reset = getattr(self._vad, 'reset', None)
        if callable(reset):
            reset()

    # -- scope of the no-speech budget ---------------------------------------

    def begin_activation(self) -> None:
        """Start a fresh activation: clear the utterance state AND the no-speech budget.

        Called once per wake or follow-up window. The budget has to start clean here, or an
        activation that ended with a full budget would close the next one on its first frame.
        """
        self.reset()

    def begin_capture(self) -> None:
        """Start another capture attempt within the same activation, keeping the budget.

        A capture attempt ends when the endpointer returns an utterance, and a rejected one is noise
        that must not buy the room more time (issue #47). `reset()` would clear the very count that
        bounds this, so the budget is carried across instead. Only a real utterance clears it, in
        `_finish`.
        """
        budget = self._frames_seen
        self.reset()
        self._frames_seen = budget

    # -- streaming ----------------------------------------------------------

    def process(self, audio: bytes) -> Optional[EndpointResult]:
        """Feed raw 16-bit mono PCM. Returns a result once an utterance completes.

        One result per call, but the feed's UNCONSUMED TAIL is never destroyed: a feed larger
        than the boundary frame (a chunked reader handing over several 1024-sample chunks at
        once) holds audio for the NEXT utterance in `self._buffer` when this one ends, and
        `_finish` resets the state machine. Without carrying that tail back, the onset of the
        next utterance -- the very class of clipping the pre-roll buffer exists to prevent -- is
        dropped on every boundary.
        """
        self._buffer.extend(audio)
        while len(self._buffer) >= self.frame_bytes:
            frame = bytes(self._buffer[: self.frame_bytes])
            del self._buffer[: self.frame_bytes]
            # Captured BEFORE the frame is processed: when the frame ends an utterance,
            # _process_frame -> _finish -> reset() replaces self._buffer, and this remainder is
            # what the next process() continues with. Captured after, it would already be gone.
            remainder = bytes(self._buffer)
            result = self._process_frame(frame)
            if result is not None:
                self._buffer.extend(remainder)  # reset() left it fresh; restore this stream's tail
                return result
        return None

    def _process_frame(self, frame: bytes) -> Optional[EndpointResult]:
        self._frames_seen += 1
        # The detector's opinion, then the level test. `observe` is called for every frame so the
        # room's floor keeps being measured even while someone is mid-sentence, and it only counts
        # frames the detector called non-speech towards that estimate.
        looks_like_speech = self._vad.is_speech(frame, self.rate)
        level = self.gate.observe(frame, looks_like_speech)
        gate_ok = self.gate.passes_rms(level)
        is_speech = looks_like_speech and gate_ok

        # Attribute the refusal, not just the outcome.
        if looks_like_speech:
            self._vad_speech_frames += 1
            self._speech_levels.append(level)
            if not gate_ok:
                self._gate_rejected_frames += 1
        if is_speech:
            self._accepted_frames += 1

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
        self._log_levels()
        # A rejected utterance is noise, not a turn, so its frames still count towards the no-speech
        # budget. Resetting the count here is why a room producing a blip every few seconds could hold
        # the stream open indefinitely: each blip ended the utterance and zeroed the clock, so
        # `no_speech` never fired and only a wall-clock cap closed it (issue #47). Real speech still
        # starts from zero, because it is the person finally saying something.
        carried = self._frames_seen if result.rejected else 0
        self.reset()
        self._frames_seen = carried
        return result

    def _log_levels(self) -> None:
        """Say who refused the frames, and what the room looked like while it happened.

        A failed turn and an empty room both end in "nothing said after the wake word", so
        without this line the two are indistinguishable from the outside - and they have
        opposite fixes. `gate_rejected` against `vad_speech` is the discriminator:

        * `vad_speech` high, `gate_rejected` nearly as high -> the level gate is deaf to this
          speaker in this room. Tuning the gate (or its ratio) is the fix.
        * `vad_speech` low -> the spectral detector never accepted the voice, and no amount of
          gate tuning will help; that is a detector/aggressiveness/audio-path problem.

        The room percentiles are printed next to it because the floor is a LOW percentile of
        non-speech frames while a fan is loud at its peaks, so p20 alone can hide an
        over-tight threshold. `thr` is what speech must beat.
        """
        gate = self.gate.stats()
        levels = sorted(self._speech_levels)
        median = levels[len(levels) // 2] if levels else 0.0
        loudest = levels[-1] if levels else 0.0
        _LOGGER.info(
            "  levels: room p20=%.0f p50=%.0f p90=%.0f max=%.0f (n=%d) | floor=%.0f thr=%.0f"
            " | frames: vad_speech=%d gate_rejected=%d accepted=%d"
            " | vad_speech_rms med=%.0f max=%.0f",
            gate["p20"], gate["p50"], gate["p90"], gate["max"], gate["samples"],
            gate["floor"], gate["threshold"],
            self._vad_speech_frames, self._gate_rejected_frames, self._accepted_frames,
            median, loudest,
        )
