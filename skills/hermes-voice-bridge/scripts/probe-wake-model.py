#!/usr/bin/env python3
"""Score a wake model with the service's own pipeline. Run ON wyoming1, with the wake service's venv:

  /home/wyoming1/wyoming-openwakeword/.venv/bin/python3 probe-wake-model.py \
      /home/wyoming1/wake-models/hey_hermes.tflite

The point of the control row: the built-in model must score HIGH on its own recording, proving the
measurement works, while a different wake word's model must score LOW on that same recording, proving
it discriminates and is not a repackaged built-in. Silence must score ~0.

Do NOT use Piper or any TTS to test a wake word: synthesised speech scores around 0.001 against these
models by design. Only a real recording of the person tells you anything.
"""
import glob
import os
import sys
import wave

import numpy as np

from pyopen_wakeword import Model, OpenWakeWordFeatures
from pyopen_wakeword.openwakeword import OpenWakeWord

FRAME = 1280  # 80 ms at 16 kHz


def score(model, audio):
    feats = OpenWakeWordFeatures.from_builtin()
    model.reset()
    peaks = []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        for emb in feats.process_streaming(audio[i:i + FRAME].tobytes()):
            peaks.extend(model.process_streaming(emb))
    feats.close()
    return round(max(peaks), 4) if peaks else None


def load_wav(path):
    with wave.open(path) as wf:
        rate = wf.getframerate()
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    if rate != 16000:
        raise SystemExit(f"{path} is {rate} Hz; these models need 16 kHz mono")
    return audio


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    if not os.path.isfile(path):
        raise SystemExit(f"no such model: {path}")

    with open(path, "rb") as fh:
        head = fh.read(8)
    print(f"model: {path} ({os.path.getsize(path)} bytes, TFLite identifier: {head[4:8] == b'TFL3'})")

    custom = OpenWakeWord.from_model(path)
    print(f"resolved wake word name: {custom.id!r}  (this is what --wake-word-name must say)")

    print()
    print("1. silence (expect ~0)")
    print("   score:", score(custom, np.zeros(16000 * 3, dtype=np.int16)))

    wavs = sorted(glob.glob("/home/wyoming1/wyoming-openwakeword/tests/**/*.wav", recursive=True))
    control = next((w for w in wavs if "nabu" in os.path.basename(w).lower()), None)
    if not control:
        print()
        print("no control recording found; silence result only")
        return

    audio = load_wav(control)
    print()
    print(f"2. control recording {os.path.basename(control)} ({len(audio)/16000:.1f}s)")
    builtin = OpenWakeWord.from_builtin(Model("okay_nabu"))
    print("   built-in okay_nabu on it (expect HIGH, validates the method):", score(builtin, audio))
    print(f"   {custom.id} on it (expect LOW, proves discrimination):", score(custom, audio))


if __name__ == "__main__":
    main()
