#!/usr/bin/env python3
"""Play the awake-tone candidates on the satellite, each announced by name.

Announced with Piper through the same bridge, so the choice is made by ear rather than by
reading frequency numbers.

    .venv/bin/python scripts/preview_awake_candidates.py

Then set ACTIVE_AWAKE in make_chimes.py, re-run it, and copy the result to the Pi. The
satellite opens the WAV per detection (satellite.py:646-669 `wav_to_events(wav_path, ...)`),
so a tone change needs no service restart.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import tts  # noqa: E402
from bridge.config import AUDIO_OUT, SATELLITE_HOST, SATELLITE_PORT  # noqa: E402
from bridge.satellite import SatelliteClient  # noqa: E402

CANDIDATES = [
    ("a-warm-fifth", "Option A: four forty, rising to six sixty."),
    ("b-bright-fifth", "Option B: five twenty three, rising to seven eighty four."),
    ("c-low-fourth", "Option C: three ninety two, rising to five twenty three."),
]


async def main() -> int:
    sounds = Path(__file__).resolve().parent.parent / "sounds"
    async with SatelliteClient(SATELLITE_HOST, SATELLITE_PORT, log_events=False) as sat:
        for name, label in CANDIDATES:
            tone = sounds / "candidates" / f"awake-{name}.wav"
            if not tone.is_file():
                print(f"missing {tone}: run scripts/make_chimes.py first")
                return 1
            wav = AUDIO_OUT / f"label-{name}.wav"
            tts.synthesize_to_wav(label, wav)
            print(f"--- {name} ---")
            await sat.play_wav(wav)
            await asyncio.sleep(0.25)
            await sat.play_wav(tone)
            await asyncio.sleep(1.3)
    print("all three played; tell me which one you want")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
