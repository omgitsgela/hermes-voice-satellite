#!/usr/bin/env python3
"""Silence after "Hey Hermes" reads as failure; the turn must fill it.

Why this exists. The filler used to fire ONLY on a tool call. A model that simply
took a while to produce its first token said nothing at all, so from the room
a slow turn looked exactly like a dead device -- while the reference satellite
implementation says "One moment." and the person knows they are being heard.

Run with the bridge's own interpreter:

    ~/hermes-voice-bridge/.venv/bin/python -B scripts/test_silence_filler.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import bridge.loop as loop_module  # noqa: E402
from bridge.endpointing import EndpointResult  # noqa: E402

Loop = next(obj for obj in vars(loop_module).values()
            if isinstance(obj, type) and hasattr(obj, "_watch_for_interruption"))

checks = 0
failures = []


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


class RecordingStream:
    def __init__(self):
        self.said = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def say(self, text):
        self.said.append(text)
        return 0.1

    @property
    def seconds_sent(self):
        return 0.1


def make_bridge(gateway_deltas, delay_before_first):
    """A loop whose gateway is slow on purpose. Everything else is a stub."""
    bridge = Loop.__new__(Loop)
    bridge.turns = []
    bridge.turn_started_at = None
    bridge._speaking = False
    bridge._speaking_at = 0.0
    bridge._held_wake = None
    bridge.follow_up = False
    bridge.leased = None
    bridge.self_trigger_count = 0
    bridge.stop_requested = False
    bridge.interrupt_enabled = False
    bridge.gateway = types.SimpleNamespace(session_id="filler-test", stop=None)
    stream = RecordingStream()
    async def next_event(_timeout):
        return None

    bridge.sat = types.SimpleNamespace(
        eof=False,
        send=lambda _e: asyncio.sleep(0),
        next_event=next_event,
        tts_stream=lambda **_kw: stream,
    )

    async def wait_for_wake():
        return "hey_hermes"

    async def capture_after_wake(_wake):
        bridge.turn_started_at = time.monotonic()
        return EndpointResult(audio=b"\0\0" * 8000, rate=16000, duration=0.5,
                              speech_seconds=0.5, first_speech_at=0,
                              speech_absolute_at=0, ended_by="silence"), ""

    async def transcribe(_audio):
        return "Hello"

    async def no_watcher(_state):
        # Not the subject here (test_interrupt_lifetime covers it), and a real one
        # would spin on a stub satellite that never produces events.
        await asyncio.sleep(3600)

    bridge._watch_for_interruption = no_watcher
    bridge.wait_for_wake = wait_for_wake
    bridge.capture_after_wake = capture_after_wake
    bridge.transcribe = transcribe
    bridge.rotate_session = lambda _reason: None
    bridge._local_command = lambda _text: ""
    bridge._volume_command = lambda _text: asyncio.sleep(0, result=None)
    bridge.publish_led = lambda *_a, **_kw: asyncio.sleep(0)
    bridge._save_state = lambda: None
    bridge._maybe_rotate_on_idle = lambda: None
    bridge._replay_buffer = lambda: []
    bridge._next = lambda _t: asyncio.sleep(0, result=None)
    bridge.wait_for_played = lambda **_kw: asyncio.sleep(0, result=True)

    async def ask(_text, on_tool_started=None):
        await asyncio.sleep(delay_before_first)
        for delta in gateway_deltas:
            yield delta

    bridge.gateway.ask = ask
    return bridge, stream


async def main() -> int:
    # 1. a slow first token is filled, exactly like a slow tool call
    bridge, stream = make_bridge(["A real answer."], delay_before_first=0.35)
    saved = loop_module.SILENCE_FILLER_SECONDS
    loop_module.SILENCE_FILLER_SECONDS = 0.1
    try:
        await asyncio.wait_for(bridge.one_turn(), timeout=5.0)
    finally:
        loop_module.SILENCE_FILLER_SECONDS = saved
    check("a slow turn says something first", "One moment." in stream.said, True)
    check("the answer still follows the filler", stream.said[-1], "A real answer.")

    # 2. a prompt turn is NOT interrupted by the filler
    bridge, stream = make_bridge(["A real answer."], delay_before_first=0.0)
    loop_module.SILENCE_FILLER_SECONDS = 5.0
    try:
        await asyncio.wait_for(bridge.one_turn(), timeout=5.0)
    finally:
        loop_module.SILENCE_FILLER_SECONDS = saved
    check("a fast turn says no filler", "One moment." in stream.said, False)
    check("a fast turn still answers", stream.said, ["A real answer."])

    print(f"silence-filler checks: {checks - len(failures)}/{checks} passed")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
