#!/usr/bin/env python3
"""A transcript that is a non-word must not become a turn (issue #47).

Observed live on 2026-09-24 at 07:06: a false wake fired, the microphone captured the device's own
wake chime, the endpointer accepted 0.3 s of it as speech, speech-to-text produced "beep", and a full
turn went to the gateway, which answered it out loud. The chime's return path is addressed in
`capture_after_wake`; this is the last line of defence, after speech-to-text has had its say.

Length cannot be the test here, which is why the interruption path's `INTERRUPT_MIN_CHARS` guard is
not reused: a wake-triggered turn has legitimate short replies ("yes", "no", "stop"), and dropping on
length alone would swallow them. The guard is therefore a whole-utterance match against a short list,
the same shape as the stop-phrase matcher, so "beep the horn" is still a question about a horn.

Run with the Gateway venv:

    .venv/bin/python scripts/test_non_word.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import satellite_gateway.loop as loop_module  # noqa: E402

Bridge = next(obj for obj in vars(loop_module).values()
              if isinstance(obj, type) and hasattr(obj, "one_turn"))

checks = 0
failures: list[str] = []


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


class ReachedGateway(Exception):
    """Raised by the stub gateway so a test can assert the transcript got that far."""


class StubGateway:
    session_id = "test-session"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def ask(self, text, on_tool_started=None):
        self.calls.append(text)

        async def _stream():
            raise ReachedGateway(text)
            yield  # pragma: no cover - makes this an async generator

        return _stream()


class StubStream:
    """Minimal stand-in for the satellite's speech stream, entered before the gateway is asked."""

    seconds_sent = 0.0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def say(self, text):
        return None


def _stream_factory():
    return StubStream()


async def run_turn(transcript: str) -> dict:
    """Drive one real one_turn() with stubs, and report what it decided."""
    gateway = Bridge.__new__(Bridge)
    gateway.gateway = StubGateway()
    gateway.sat = types.SimpleNamespace(send=_noop_send, eof=False, tts_stream=_stream_factory)
    gateway.turns = []
    gateway.turn_started_at = None
    gateway._speaking = False
    gateway.self_trigger_count = 0
    gateway.follow_up = False
    gateway._last_turn_at = None
    gateway.idle_reset_minutes = 0          # so the idle-rotation check short-circuits
    gateway._state_file = pathlib.Path("/tmp/non-word-test-state")

    spoke: list[str] = []
    led: list[str] = []

    async def wait_for_wake():
        return "hey_hermes"

    async def capture_after_wake(wake_word):
        endpoint = types.SimpleNamespace(audio=b"\x00" * 3200, rejected=False,
                                         ended_by="silence", speech_seconds=0.3)
        return endpoint, ""

    async def transcribe(audio):
        return transcript

    async def say(text):
        spoke.append(text)

    async def publish_led(name):
        led.append(name)

    async def rearm():
        pass

    async def forever(state):
        await asyncio.sleep(30)

    gateway.wait_for_wake = wait_for_wake
    gateway.capture_after_wake = capture_after_wake
    gateway.transcribe = transcribe
    gateway.say = say
    gateway.publish_led = publish_led
    gateway._rearm_wake_word = rearm
    gateway._watch_for_interruption = forever
    gateway._next = lambda timeout: _none()
    # The real matcher, so the test exercises production classification, not a reimplementation.
    gateway._local_command = Bridge._local_command.__get__(gateway, Bridge)

    stats = None
    try:
        stats = await gateway.one_turn()
    except ReachedGateway:
        pass

    return {
        "reached_gateway": bool(gateway.gateway.calls),
        "spoke": spoke,
        "note": getattr(stats, "note", None) if stats is not None else None,
        "gate_raised": gateway.turn_started_at is not None,
        "led": led,
    }


async def _none():
    return None


async def _noop_send(event):
    return None


async def main() -> int:
    # The words the device's own chime produces, in the form speech-to-text returns them.
    for word in ("beep", "Beep", "beep.", "boop", "ding", "bleep", "blip"):
        result = await run_turn(word)
        check(f"a chime word ({word!r}) never reaches the gateway", result["reached_gateway"], False)
        check(f"and the device stays silent for {word!r}", result["spoke"], [])
        check(f"and it is recorded as a non-word for {word!r}", result["note"], "non-word transcript")
        check(f"and the announcement gate is not left raised for {word!r}",
              result["gate_raised"], False)

    # Legitimate short replies must still get through: they are the reason length is not the test.
    for text in ("yes", "no", "what's the weather", "beep the horn", "ding the car again",
                 "yes please"):
        result = await run_turn(text)
        check(f"a real reply ({text!r}) still reaches the gateway", result["reached_gateway"], True)

    # And a short *control* is answered locally, without a model call, which is the other reason a
    # length guard cannot be reused here: "stop" is four characters and must keep working.
    result = await run_turn("stop")
    check("a short control does not reach the gateway", result["reached_gateway"], False)
    check("and it is answered locally", result["spoke"], ["Stopping."])

    result = await run_turn("beep boop")
    check("two chime words together still reach the gateway (no substring matching)",
          result["reached_gateway"], True)

    print(f"non-word guard checks: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
