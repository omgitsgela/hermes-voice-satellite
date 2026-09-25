#!/usr/bin/env python3
"""The announcement gate while the microphone is open (issue #19).

Announcements must not play while the device is listening: the microphone is open, so the
announcement is captured and transcribed as if the person had said it. The gate is
`turn_started_at`, read by the announcer through its own predicate. Run with the bridge venv:

    .venv/bin/python scripts/test_announce_gate.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import bridge.loop as loop_module  # noqa: E402
from bridge.announce import Announcer  # noqa: E402
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


class DeadSat:
    """A satellite whose connection is gone, so capture_utterance raises instead of looping."""

    eof = True

    async def next_event(self, timeout):
        return None


def make_bridge():
    bridge = Loop.__new__(Loop)
    bridge.sat = DeadSat()
    bridge.turn_started_at = None
    bridge._ep = types.SimpleNamespace(reset=lambda: None)
    return bridge


def gate_is_active(bridge) -> bool:
    """Ask the real announcer predicate, not a copy of its logic."""
    probe = Announcer.__new__(Announcer)
    probe.loop = bridge
    # the announcer takes a callable here, not a value: it is built without a loop in some
    # deployments, so the timestamp is injected as a function
    probe._turn_started_at = lambda: getattr(bridge, "turn_started_at", None)
    return probe._turn_is_active()


def make_early_return_bridge(transcript: str, local_reply: str = ""):
    bridge = Loop.__new__(Loop)
    bridge.gateway = types.SimpleNamespace(session_id="announce-gate-test")
    bridge.turns = []
    bridge.turn_started_at = None
    bridge._speaking = False
    bridge.follow_up = False
    bridge.leased = None

    class Sat:
        async def send(self, _event):
            pass

    bridge.sat = Sat()

    async def wait_for_wake():
        return "hey_hermes"

    async def capture_after_wake(_wake_word):
        bridge.turn_started_at = time.monotonic()
        return EndpointResult(
            audio=b"\0\0" * 8000,
            rate=16000,
            duration=0.5,
            speech_seconds=0.5,
            first_speech_at=0,
            speech_absolute_at=0,
            ended_by="silence",
        ), ""

    async def transcribe(_audio):
        return transcript

    async def say(_text):
        pass

    bridge.wait_for_wake = wait_for_wake
    bridge.capture_after_wake = capture_after_wake
    bridge.transcribe = transcribe
    bridge.say = say
    bridge._local_command = lambda _text: local_reply
    bridge.rotate_session = lambda _reason: None
    return bridge


async def main() -> int:
    # 1. opening the microphone raises the gate
    bridge = make_bridge()
    check("gate down before anyone speaks", gate_is_active(bridge), False)
    try:
        await bridge.capture_utterance("hey_hermes")
    except Exception:
        pass
    check("gate up while listening", gate_is_active(bridge), True)

    # 2. an existing turn's timestamp is not overwritten, so the stale-age net keeps counting from
    #    the turn's start rather than from each capture (an interruption during a long task)
    bridge = make_bridge()
    started = time.monotonic() - 42.0
    bridge.turn_started_at = started
    try:
        await bridge.capture_utterance("hey_hermes")
    except Exception:
        pass
    check("existing turn timestamp preserved", bridge.turn_started_at, started)

    # 3. with no turn, the announcer sees an idle room
    bridge = make_bridge()
    check("idle room is not gated", gate_is_active(bridge), False)

    # 4. Every successful early return after capture releases the gate (#51).
    cases = (
        ("empty transcript", "", ""),
        ("local control", "is follow up mode on", "Follow-up mode is off."),
        ("spoken session reset", "start a new conversation", ""),
    )
    for label, transcript, local_reply in cases:
        bridge = make_early_return_bridge(transcript, local_reply)
        await bridge.one_turn()
        check(f"{label} clears timestamp", bridge.turn_started_at, None)
        check(f"{label} releases announcement gate", gate_is_active(bridge), False)

    print(f"announce gate checks: {checks - len(failures)}/{checks} passed")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
