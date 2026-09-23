#!/usr/bin/env python3
"""Transcribe a WAV with local faster-whisper, and report the numbers behind the result.

Built to answer a specific question with evidence rather than impressions: "the mics are
buried in the case and the recording sounds a bit quiet, can STT actually hear it?"

    .venv/bin/python scripts/transcribe_wav.py audio/mic-....wav
    .venv/bin/python scripts/transcribe_wav.py audio/mic-....wav --gain 4
    .venv/bin/python scripts/transcribe_wav.py audio/mic-....wav --vad

Reports the peak and the SPEECH level (RMS over the loudest quarter of 20 ms frames, which
ignores the surrounding silence that would otherwise flatter the figure), then the
transcript with per-segment confidence. Watching the transcript change as `--gain` rises
shows whether the level is the limiting factor.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

TARGET_RATE = 16000  # what whisper wants


def load_wav_16k_mono(path: str) -> np.ndarray:
    """16-bit PCM in, float32 mono at 16 kHz out."""
    with wave.open(path, "rb") as wf:
        rate, width, channels = wf.getframerate(), wf.getsampwidth(), wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise SystemExit(f"expects 16-bit PCM, got {width * 8}-bit")
    audio = np.frombuffer(raw, dtype="<i2").astype("f4")
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != TARGET_RATE:
        n = int(len(audio) * TARGET_RATE / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n),
                          np.arange(len(audio)), audio).astype("f4")
    return audio


def speech_level(audio: np.ndarray) -> tuple[float, float]:
    """(peak, speech_rms) from the loudest quarter of 20 ms frames, so silence cannot flatter it."""
    frame = 320  # 20 ms at 16 kHz
    frames = [audio[i:i + frame] for i in range(0, max(len(audio) - frame, 1), frame)]
    rms = np.array([float(np.sqrt(np.mean(f ** 2))) for f in frames]) if frames else np.array([0.0])
    loudest = np.sort(rms)[-max(1, len(rms) // 4):]
    return float(np.abs(audio).max()), float(loudest.mean())


def dbfs(value: float) -> float:
    return 20 * np.log10(value / 32768.0) if value > 0 else float("-inf")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--model", default="small.en")
    ap.add_argument("--gain", type=float, default=1.0, help="linear gain applied before STT")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--vad", action="store_true", help="let whisper trim silence first")
    args = ap.parse_args()

    if not Path(args.wav).is_file():
        raise SystemExit(f"no such file: {args.wav}")

    audio = load_wav_16k_mono(args.wav)
    peak, rms = speech_level(audio)
    print(f"file       : {args.wav}")
    print(f"duration   : {len(audio) / TARGET_RATE:.2f} s at {TARGET_RATE} Hz mono")
    print(f"peak       : {peak:.0f} ({dbfs(peak):.1f} dBFS)")
    print(f"speech RMS : {rms:.0f} ({dbfs(rms):.1f} dBFS)   <- loudest quarter of 20 ms frames")
    if args.gain != 1.0:
        audio = np.clip(audio * args.gain, -32768, 32767)
        g_peak, g_rms = speech_level(audio)
        print(f"after gain : x{args.gain} -> peak {dbfs(g_peak):.1f} dBFS, speech RMS {dbfs(g_rms):.1f} dBFS")

    from faster_whisper import WhisperModel

    print(f"model      : {args.model}, int8, {args.threads} threads, vad={args.vad}")
    model = WhisperModel(args.model, device="cpu", compute_type="int8",
                         cpu_threads=args.threads)
    segments, info = model.transcribe(
        (audio / 32768.0).astype("f4"), language="en", beam_size=1, vad_filter=args.vad,
    )
    print(f"detected   : {info.language} (p={info.language_probability:.2f}), "
          f"audio {info.duration:.2f} s")
    print("transcript :")
    any_segment = False
    for seg in segments:
        any_segment = True
        print(f"   [{seg.start:5.2f}-{seg.end:5.2f}] {seg.text.strip()!r}")
        print(f"            avg_logprob {seg.avg_logprob:.3f}  "
              f"no_speech_prob {seg.no_speech_prob:.3f}")
    if not any_segment:
        print("   (NOTHING TRANSCRIBED: STT could not find speech in this audio)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
