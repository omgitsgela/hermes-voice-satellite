#!/usr/bin/env python3
"""Phase 2, test B: synthesize a phrase with Piper and play it on the satellite.

No wake word needed. The satellite forwards AudioStart/Chunk/Stop to aplay, but it must be armed
with RunSatellite first: a satellite that is still streaming can ignore the first message it
receives, so this arms before playing anything (issue #37).

    .venv/bin/python scripts/play_phrase.py
    .venv/bin/python scripts/play_phrase.py --text "Your custom phrase here"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import tts  # noqa: E402
from gateway.config import AUDIO_OUT, SATELLITE_HOST, SATELLITE_PORT  # noqa: E402
from gateway.satellite import SatelliteClient  # noqa: E402

DEFAULT_TEXT = (
    "Hermes here. This is the voice Gateway test. "
    "If you can hear this through the satellite speaker, the audio path works."
)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--host", default=SATELLITE_HOST)
    parser.add_argument("--port", type=int, default=SATELLITE_PORT)
    parser.add_argument("--keep-wav", action="store_true", default=True,
                        help="keep the synthesized WAV as an artifact")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    wav_path = AUDIO_OUT / f"phrase-{stamp}.wav"

    print("=== 1. SYNTHESIZE (Piper, local) ===")
    tts.synthesize_to_wav(args.text, wav_path)
    info = tts.describe_wav(wav_path)
    print(f"  {info['path']}")
    print(f"  {info['rate']} Hz, {info['width']} bytes, {info['channels']} ch, "
          f"{info['seconds']:.2f} s")
    if info["rate"] != 22050 or info["width"] != 2 or info["channels"] != 1:
        print("  WARNING: not 22050/2/1; the satellite's aplay expects exactly that")

    print("\n=== 2. CONNECT TO THE SATELLITE ===")
    async with SatelliteClient(args.host, args.port) as sat:
        info_msg = await sat.describe()
        if info_msg is not None:
            sat_name = getattr(getattr(info_msg, "satellite", None), "name", None)
            wake_names = [getattr(w, "name", None) or str(w)
                          for w in (getattr(info_msg, "wake", None) or [])]
            mic_names = [getattr(m, "name", None) or str(m)
                         for m in (getattr(info_msg, "mic", None) or [])]
            snd_names = [getattr(s, "name", None) or str(s)
                         for s in (getattr(info_msg, "snd", None) or [])]
            print(f"  satellite name : {sat_name}")
            print(f"  wake programs  : {wake_names}")
            print(f"  mic services   : {mic_names}")
            print(f"  snd services   : {snd_names}")
            print(f"  raw            : {info_msg!r}"[:400])

        # The satellite plays for the connection it treats as its client. The Gateway is that client
        # while it runs, so playback from here is ignored and this script reports a false failure on
        # a healthy system. Warn rather than let someone chase a hardware fault. (Issue #37.)
        bridge_up = subprocess.run(["systemctl", "--user", "is-active", "hermes-voice-satellite"],
                                   capture_output=True, text=True).stdout.strip()
        if bridge_up == "active":
            print("  NOTE: hermes-voice-satellite is running and holds the satellite's client connection.")
            print("        Playback from this second connection is likely to be ignored, so a result")
            print("        of 'not played' below may say nothing about the audio path. Stop the Gateway")
            print("        first, or announce through it instead.")

        print("\n=== 3. ARM THE SATELLITE, THEN STREAM IT TO THE SPEAKER ===")
        # Arm it the way the Gateway and scripts/diag_followup_stream.py do. A satellite that is still
        # streaming can ignore the first message it receives, so playback sent without RunSatellite is
        # not reliably heard. This script is the sole connection, so arming here carries none of the
        # second-connection risk that keeps gateway/announce.py from sending it (issue #37).
        await sat.run()
        try:
            played = await asyncio.wait_for(sat.play_wav(wav_path), timeout=45.0)
        except asyncio.TimeoutError:
            print("\n=== RESULT: no Played event within 45s ===")
            print("  the audio was sent but the satellite never confirmed playback; see the note above")
            return 1
        print(f"\n=== RESULT: satellite reported Played = {played} ===")
        print("  events seen:", sat.event_counts)
        if not played:
            print("  (no Played event; the audio was sent but playback was not confirmed)")
            return 1
    print("\nListen for the phrase. If it was audible, Phase 2 playback is proven.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
