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

    print(f"announce gate checks: {checks - len(failures)}/{checks} passed")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
