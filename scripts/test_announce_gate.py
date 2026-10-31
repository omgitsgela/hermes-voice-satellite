#!/usr/bin/env python3
"""The announcement gate while the microphone is open (issue #19).

Announcements must not play while the device is listening: the microphone is open, so the
announcement is captured and transcribed as if the person had said it. The gate is
`turn_started_at`, read by the announcer through its own predicate. Run with the Gateway venv:

    .venv/bin/python scripts/test_announce_gate.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import satellite_gateway.loop as loop_module  # noqa: E402
from satellite_gateway.announce import Announcer  # noqa: E402
from satellite_gateway.endpointing import EndpointResult  # noqa: E402

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
    gateway = Loop.__new__(Loop)
    gateway.sat = DeadSat()
    gateway.turn_started_at = None
    gateway._ep = types.SimpleNamespace(reset=lambda: None)
    return Gateway


def gate_is_active(gateway) -> bool:
    """Ask the real announcer predicate, not a copy of its logic."""
    probe = Announcer.__new__(Announcer)
    probe.loop = Gateway
    # the announcer takes a callable here, not a value: it is built without a loop in some
    # deployments, so the timestamp is injected as a function
    probe._turn_started_at = lambda: getattr(gateway, "turn_started_at", None)
    return probe._turn_is_active()


def make_early_return_bridge(transcript: str, local_reply: str = ""):
    gateway = Loop.__new__(Loop)
    gateway.gateway = types.SimpleNamespace(session_id="announce-gate-test")
    gateway.turns = []
    gateway.turn_started_at = None
    gateway._speaking = False
    gateway.follow_up = False
    gateway.leased = None

    class Sat:
        async def send(self, _event):
            pass

    gateway.sat = Sat()

    async def wait_for_wake():
        return "hey_hermes"

    async def capture_after_wake(_wake_word):
        gateway.turn_started_at = time.monotonic()
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

    gateway.wait_for_wake = wait_for_wake
    gateway.capture_after_wake = capture_after_wake
    gateway.transcribe = transcribe
    gateway.say = say
    gateway._local_command = lambda _text: local_reply
    gateway.rotate_session = lambda _reason: None
    return Gateway


async def main() -> int:
    # 1. opening the microphone raises the gate
    gateway = make_bridge()
    check("gate down before anyone speaks", gate_is_active(gateway), False)
    try:
        await gateway.capture_utterance("hey_hermes")
    except Exception:
        pass
    check("gate up while listening", gate_is_active(gateway), True)

    # 2. an existing turn's timestamp is not overwritten, so the stale-age net keeps counting from
    #    the turn's start rather than from each capture (an interruption during a long task)
    gateway = make_bridge()
    started = time.monotonic() - 42.0
    gateway.turn_started_at = started
    try:
        await gateway.capture_utterance("hey_hermes")
    except Exception:
        pass
    check("existing turn timestamp preserved", gateway.turn_started_at, started)

    # 3. with no turn, the announcer sees an idle room
    gateway = make_bridge()
    check("idle room is not gated", gate_is_active(gateway), False)

    # 4. Every successful early return after capture releases the gate (#51).
    cases = (
        ("empty transcript", "", ""),
        ("local control", "is follow up mode on", "Follow-up mode is off."),
        ("spoken session reset", "start a new conversation", ""),
    )
    for label, transcript, local_reply in cases:
        gateway = make_early_return_bridge(transcript, local_reply)
        await gateway.one_turn()
        check(f"{label} clears timestamp", gateway.turn_started_at, None)
        check(f"{label} releases announcement gate", gate_is_active(gateway), False)

    print(f"announce gate checks: {checks - len(failures)}/{checks} passed")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
