#!/usr/bin/env python3
"""Phase 2, test A: capture mic audio from the satellite, optionally play it back.

This NEEDS a real wake word. Per `WakeStreamingSatellite.event_from_mic`, mic chunks are
only forwarded while the satellite is streaming, and only the local wake word starts it.
There is no protocol way to ask for mic audio on demand.

    .venv/bin/python scripts/capture_mic.py --seconds 5
    .venv/bin/python scripts/capture_mic.py --seconds 5 --playback

`--playback` sends the captured WAV straight back to the satellite's speaker, which proves
the complete audio round trip (mic -> network -> WAV -> speaker) with no LLM involved.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from satellite_gateway.config import AUDIO_OUT, SATELLITE_HOST, SATELLITE_PORT  # noqa: E402
from satellite_gateway.satellite import SatelliteClient  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--wait", type=float, default=180.0,
                        help="how long to wait for the wake word before giving up")
    parser.add_argument("--host", default=SATELLITE_HOST)
    parser.add_argument("--port", type=int, default=SATELLITE_PORT)
    parser.add_argument("--playback", action="store_true",
                        help="play the captured audio back through the satellite speaker")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    out = AUDIO_OUT / f"mic-{datetime.now().strftime('%Y%m%d-%H%M%S')}.wav"

    async with SatelliteClient(args.host, args.port) as sat:
        info = await sat.describe()
        if info is not None:
            name = getattr(getattr(info, "satellite", None), "name", "?")
            print(f"connected to satellite: {name}")

        print(f"\n>>> Say the wake word now, then speak for {args.seconds:.0f} seconds. <<<\n")
        await sat.run()
        try:
            result = await sat.capture(out, seconds=args.seconds, max_wait=args.wait)
        except (TimeoutError, ConnectionError, RuntimeError) as exc:
            print(f"CAPTURE FAILED: {type(exc).__name__}: {exc}")
            return 1

        print("=== CAPTURE RESULT ===")
        print(f"  file          : {result.path}")
        print(f"  duration      : {result.seconds:.2f} s in {result.chunks} chunks")
        print(f"  format        : {result.rate} Hz, {result.width * 8}-bit, {result.channels} ch")
        print(f"  peak / RMS    : {result.peak} ({result.dbfs:.1f} dBFS) / {result.rms:.0f}")
        print(f"  wake word seen: {result.detected}")
        print(f"  run-pipeline  : {result.saw_run_pipeline}")

        # Onset diagnostic. The satellite mutes the mic for (awake wav duration +
        # --mic-seconds-to-mute-after-awake-wav), so if she speaks as the tone ends, the
        # mute eats the front of the utterance BEFORE it is ever recorded. A recording that
        # opens at full level with no lead-in is the signature of that.
        import wave as _wave

        with _wave.open(str(result.path), "rb") as _wf:
            _s = np.frombuffer(_wf.readframes(_wf.getnframes()), dtype="<i2").astype("f8")
        loud = np.where(np.abs(_s) > max(result.peak * 0.05, 100))[0]
        onset = float(loud[0]) / result.rate if loud.size else float("nan")
        first_10ms = int(np.abs(_s[: result.rate // 100]).max()) if _s.size else 0
        print(f"  onset         : first loud sample at {onset:.3f} s; "
              f"first-10ms peak={first_10ms}")
        if onset < 0.02:
            print("  NOTE: the recording opens at full level with no lead-in. That is the")
            print("        satellite's mic mute clipping the onset of speech, not a playback")
            print("        fault. Lower --mic-seconds-to-mute-after-awake-wav.")
        if result.peak < 500:
            print("  WARNING: very quiet. Check the mic, or try --mic-auto-gain on the satellite.")
        if not result.saw_run_pipeline:
            print("  WARNING: no run-pipeline event; the satellite may not have streamed properly.")

        if args.playback:
            print("\n=== PLAYING IT BACK THROUGH THE SATELLITE SPEAKER ===")
            played = await sat.play_wav(result.path)
            print(f"  satellite reported Played = {played}")
            if not played:
                return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
