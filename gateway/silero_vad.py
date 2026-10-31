"""A local, vendored Silero voice detector (ONNX), owned by this repository.

Why this replaced `webrtcvad` is in `scripts/test_silero_vad.py` and `docs/voice-detection.md`.
The one-line version: webrtcvad calls ~99% of a noisy room "speech", which is why the project
needed an aggressive energy gate, and the gate then went deaf to the person in an office.

The model lives in `gateway/vendor/silero_vad_v6.onnx` and is loaded with `onnxruntime`, which the
Gateway already installs. Nothing here reaches the network, and nothing here reads an asset out of
another package's wheel.

## Streaming

The endpointer feeds 20 ms frames (320 samples at 16 kHz) and Silero works in 512-sample windows
with 64 samples of context carried between them, so frames are buffered into windows. A call that
does not complete a window returns the most recent decision rather than raising, because the
endpointer has no idea where the window boundaries fall.

`is_speech` deliberately mirrors `webrtcvad.Vad.is_speech`, so the two are interchangeable at the
call site and the choice is configuration rather than a different code path.
"""

from __future__ import annotations

import array
import logging
import pathlib
from typing import Optional

import numpy as np

_LOGGER = logging.getLogger("gateway.silero_vad")

MODEL_PATH = pathlib.Path(__file__).resolve().parent / "vendor" / "silero_vad_v6.onnx"

#: Silero v6 consumes 512-sample windows with 64 samples of carried context.
WINDOW_SAMPLES = 512
CONTEXT_SAMPLES = 64
#: LSTM state carried between windows.
STATE_DIM = 128
#: The v6 ONNX export is a 16 kHz model. There is a separate 8 kHz variant we do not ship.
SUPPORTED_RATES = (16000,)


class SileroVad:
    """Voice activity from a vendored ONNX model. Same interface as `webrtcvad.Vad`."""

    def __init__(self, model_path: pathlib.Path = MODEL_PATH, rate: int = 16000,
                 threshold: float = 0.5) -> None:
        if rate not in SUPPORTED_RATES:
            raise ValueError(
                f"the vendored Silero model is {SUPPORTED_RATES[0]} Hz; got {rate}. "
                "Resampling here would hide a rate mistake rather than fix it."
            )
        if not model_path.is_file():
            raise FileNotFoundError(
                f"vendored Silero model missing at {model_path}. It is tracked in this "
                "repository; a missing file means a checkout or packaging problem, not a "
                "missing download."
            )

        import onnxruntime

        opts = onnxruntime.SessionOptions()
        # One thread each, no arena: this runs inside an asyncio service on a small host, and a
        # pool would fight the rest of the Gateway for cores on every 20 ms frame.
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.enable_cpu_mem_arena = False
        opts.log_severity_level = 4

        self.rate = rate
        self.threshold = threshold
        self.model_path = model_path
        self._session = onnxruntime.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"], sess_options=opts,
        )
        self.reset()

    # -- state ---------------------------------------------------------------

    def reset(self) -> None:
        """Forget the audio and the LSTM state. Called between utterances."""
        self._buffer = array.array("h")
        self._h = np.zeros((1, 1, STATE_DIM), dtype="float32")
        self._c = np.zeros((1, 1, STATE_DIM), dtype="float32")
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype="float32")
        self._last_probability = 0.0

    # -- streaming -----------------------------------------------------------

    def is_speech(self, frame: bytes, rate: int) -> bool:
        """Whether the most recent complete window sounded like a voice.

        Mirrors `webrtcvad.Vad.is_speech`, including taking the rate, so the endpointer can swap
        detectors without knowing which it has.
        """
        if rate != self.rate:
            raise ValueError(f"expected {self.rate} Hz frames, got {rate}")

        usable = len(frame) - (len(frame) % 2)
        if usable > 0:
            samples = array.array("h")
            samples.frombytes(frame[:usable])
            self._buffer.extend(samples)

        # Run every window that is now complete. More than one can complete when a caller hands
        # over a large block, and dropping the extras would silently skip audio.
        while len(self._buffer) >= WINDOW_SAMPLES:
            window = array.array("h", self._buffer[:WINDOW_SAMPLES])
            del self._buffer[:WINDOW_SAMPLES]
            self._last_probability = self._run_window(window)

        return self._last_probability > self.threshold

    @property
    def probability(self) -> float:
        """The last window's speech probability, for diagnostics."""
        return self._last_probability

    def _run_window(self, window: "array.array[int]") -> float:
        audio = np.frombuffer(window.tobytes(), dtype="<i2").astype(np.float32) / 32768.0
        # The model takes the window with the previous window's tail prepended: 512 + 64 = 576.
        model_input = np.concatenate([self._context, audio.reshape(1, -1)], axis=1)
        probabilities, self._h, self._c = self._session.run(
            None, {"input": model_input, "h": self._h, "c": self._c},
        )
        self._context = audio[-CONTEXT_SAMPLES:].reshape(1, -1).astype(np.float32)
        return float(probabilities.reshape(-1)[0])


def make_vad(name: str, *, rate: int = 16000, aggressiveness: int = 2,
             threshold: float = 0.5) -> Optional[object]:
    """Build a detector by name.

    `silero` is the default because the measurement in `scripts/test_silero_vad.py` is not close.
    `webrtcvad` stays reachable: it is what every earlier measurement in this project was taken
    with, and being able to run the old detector makes a comparison possible instead of an
    argument.
    """
    if name == "silero":
        return SileroVad(rate=rate, threshold=threshold)
    if name == "webrtcvad":
        import webrtcvad

        return webrtcvad.Vad(aggressiveness)
    raise ValueError(f"unknown voice detector {name!r}; expected 'silero' or 'webrtcvad'")
