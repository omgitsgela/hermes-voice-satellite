#!/usr/bin/env python3
"""Does the satellite stream microphone audio with no wake word?

Angela, 2026-09-22, after follow-up mode "never got answered": the bridge's follow-up windows opened
and then timed out with nothing heard. This settles why.

The suspicion, from reading the satellite's own code: with no VAD flag, a wyoming satellite only
opens its microphone stream after a wake-word Detection. A wake is not involved in a follow-up
window, so the window has no audio to listen to at all. The earlier runs that appeared to work
captured speech that arrived with an awake chime in the journal, i.e. a wake really had happened and
the bridge absorbed the Detection while capturing.

This drives the satellite directly, with the bridge stopped, so it needs no human, no wake word and no
network beyond the Pi:

    1. connect, RunSatellite, as the bridge does;
    2. send Transcript("") exactly as the bridge does when an utterance ends;
    3. listen for AudioChunk events, and play a real spoken wav through the satellite's own speaker
       so the microphone has genuine speech to hear;
    4. report how much audio arrived.

With `--vad` absent the answer is zero. With `--vad` present, the speech should start a stream.

    systemctl --user stop hermes-voice-bridge.service
    .venv/bin/python scripts/diag_followup_stream.py
    systemctl --user start hermes-voice-bridge.service
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wyoming.asr import Transcript  # noqa: E402
from wyoming.audio import AudioChunk  # noqa: E402

from bridge.config import SATELLITE_HOST, SATELLITE_PORT  # noqa: E402
from bridge.satellite import SatelliteClient  # noqa: E402

SPOKEN_WAV = Path("/home/angela/hermes-voice-bridge/audio/announce-an-074311-3467-full.wav")
LISTEN_SECONDS = 14.0
QUIET = "--quiet" in sys.argv   # no playback: one reader only, so chunk timing is meaningful


async def main() -> int:
    if not SPOKEN_WAV.is_file():
        print(f"  no spoken wav at {SPOKEN_WAV}")
        return 2

    client = SatelliteClient(SATELLITE_HOST, SATELLITE_PORT)
    await client.connect()
    info = await client.describe()
    name = getattr(getattr(info, "satellite", None), "name", "?")
    print(f"connected to satellite: {name}")
    await client.run()

    # The bridge's own end-of-utterance signal. This is the exact state a follow-up window starts
    # from: no wake active, the device is waiting for the next thing it hears.
    await client.send(Transcript(text="").event())
    print("sent Transcript(''); mic should be idle until it hears speech")

    chunks = 0
    bytes_in = 0
    started = time.monotonic()
    played_at: float | None = None
    # Play real speech out of the satellite's own speaker after two seconds of quiet, so the
    # microphone has something genuine to hear. The speaker and the mic are on the same device,
    # which is the same loopback the endpointer sees in a real follow-up.
    play_task: asyncio.Task | None = None

    results: list[str] = []

    async def quiet() -> None:
        pass

    async def play_soon() -> None:
        nonlocal played_at
        for at in (2.0, 6.0, 10.0):
            await asyncio.sleep(at - (time.monotonic() - started))
            played_at = time.monotonic() - started
            ok = await client.play_wav(SPOKEN_WAV)
            results.append(f"t+{played_at:.1f}s play_wav returned {ok}")
            print(f"  t+{played_at:5.1f}s  played the wav, satellite confirmed: {ok}")

    # In quiet mode nothing is played: a second concurrent reader (play_wav waits for its own
    # Played event) would swallow audio chunks and make the timing look wrong.
    play_task = asyncio.create_task(quiet() if QUIET else play_soon())

    while time.monotonic() - started < LISTEN_SECONDS:
        event = await client.next_event(1.0)
        if event is None:
            continue
        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            chunks += 1
            bytes_in += len(chunk.audio)
            if chunks in (1, 50) or chunks % 200 == 0:
                print(f"  t+{time.monotonic() - started:5.1f}s  chunk #{chunks}", flush=True)

    await play_task
    audio_seconds = bytes_in / (16000 * 2)
    print(f"\nspeech played at t+{played_at:.1f}s" if played_at else "\nno speech played")
    print(f"chunks received : {chunks}")
    print(f"audio received  : {audio_seconds:.2f} s of microphone audio")
    verdict = ("MIC STREAMS WITHOUT A WAKE" if chunks else
               "NO AUDIO WITHOUT A WAKE (the follow-up window has nothing to hear)")
    for line in results:
        print(f"  {line}")
    print(f"verdict         : {verdict}")
    await client.close()
    return 0 if chunks else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
