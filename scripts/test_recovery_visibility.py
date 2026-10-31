#!/usr/bin/env python3
"""A recovered link must say so in the journal.

Run with the Gateway's own interpreter:

    ~/hermes-voice-satellite/.venv/bin/python scripts/test_recovery_visibility.py

Why this exists. `listening (session ...)` is logged exactly once, by `VoiceLoop.run()`, before the
turn loop. `_reacquire_after_loss` runs *inside* that loop and then continues it, so after a device
is power-cycled the journal ends at the re-acquire line and stops. Nothing distinguishes "recovered
and idle, waiting for a wake word" from "stuck in the recovery path".

That is not hypothetical. Measured 2026-09-26: after a satellite power-cycle the Gateway re-acquired
the lease and went quiet, and three minutes were spent deciding whether it had actually come back.
Idle is silent by design, so the journal has to state that it is listening again, or nobody reading
it can tell.

The check drives the real `_reacquire_after_loss` body, substituting only the two collaborators that
would need live hardware (the acquisition and the satellite), which is the seam this repository
already uses for reconnect tests.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gateway.loop import VoiceLoop  # noqa: E402

checks = 0
failures: list[str] = []


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


class Capture(logging.Handler):
    """Collect what the loop says, so a message can be asserted instead of eyeballed."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class QuietSatellite:
    """A satellite that neither connects nor complains."""

    async def close(self) -> None:
        pass

    async def run(self) -> None:
        pass


async def main() -> int:
    loop = VoiceLoop(api_key="test")
    session = loop.gateway.session_id

    async def fake_acquire() -> None:
        # Stand in for the acquisition: it is the part that needs a real device to dial in.
        loop.leased = "satellite"
        loop.sat = QuietSatellite()

    loop._acquire_a_satellite = fake_acquire

    logger = logging.getLogger("gateway.loop")
    capture = Capture()
    previous_level = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    try:
        await loop._reacquire_after_loss(ConnectionResetError("satellite went away"))
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)

    joined = "\n".join(capture.messages)

    # 1. The recovery path still reports what it lost.
    check("reports the loss", any("re-acquiring rather than exiting" in m for m in capture.messages), True)

    # 2. It must state that the loop is listening again, not just that it re-acquired.
    check(
        "says it is listening again",
        any("listening again" in m for m in capture.messages),
        True,
    )

    # 3. And it must name the session, so the line can be tied to the conversation in play.
    check("names the session", session in joined, True)

    print(f"recovery-visibility checks: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print("  FAIL:", failure)
    if not failures:
        print("  (messages seen:", "; ".join(capture.messages) + ")")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
