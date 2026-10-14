"""Regression test for the deaf-device bug of 2026-09-23.

The bug, in one sentence: the interruption watcher awaited a capture with no deadline, so when a
wake word arrived while the device was still speaking, the satellite had no microphone to stream,
the capture never returned, and the watcher kept ownership of the satellite event queue, silently
eating every later wake detection. The device looked completely deaf and nothing appeared in the log
at default verbosity.

This test pins the property that was missing: **a watcher whose capture never returns must not stay
inside that capture.** It uses a stub whose capture hangs forever, and asserts the watcher comes
back out to its read loop instead of disappearing into it.

Run: ./.venv/bin/python scripts/test_interrupt_lifetime.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge.config as config  # noqa: E402
from bridge.loop import VoiceLoop  # noqa: E402
from wyoming.wake import Detection  # noqa: E402

FAILURES: list[str] = []


class State:
    def __init__(self) -> None:
        self.done = False
        self.stop_playback = False


class HangingLoop:
    """A loop whose capture never returns, the exact shape of the bug."""

    def __init__(self) -> None:
        self.reads = 0
        self.captures = 0
        self.rearmed = 0
        self.eof = False
        self.held_wake = None

    def _hold_wake(self, name):
        # A wake the watcher cannot use is kept for the next turn rather than
        # dropped (see VoiceLoop._hold_wake). The stub records it so the test can
        # see the watcher continued instead of vanishing into the capture.
        self.held_wake = name

    @property
    def sat(self):
        # The interruption watcher reads the satellite directly, so it cannot consume a
        # Played event it just handed back to the turn's replay buffer (issue #82).
        return self

    async def next_event(self, timeout: float):
        self.reads += 1
        if self.reads == 1:
            # The mid-task wake word that started it: the device was still speaking.
            return Detection(name="hey_hermes").event()
        await asyncio.sleep(timeout)
        return None

    async def capture_utterance(self, wake_word: str):
        self.captures += 1
        await asyncio.sleep(3600)      # no deadline of its own, which is the trap
        return None

    async def transcribe(self, audio):
        return ""                      # never reached

    async def _end_capture(self):
        # The fix for the deaf-device bug: an abandoned capture must hand the microphone back.
        self.rearmed += 1


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


async def main() -> int:
    original = config.INTERRUPT_CAPTURE_SECONDS
    config.INTERRUPT_CAPTURE_SECONDS = 0.5     # so the test takes half a second, not twelve
    try:
        print("the watcher must not disappear into a capture that never returns")

        loop = HangingLoop()
        state = State()
        task = asyncio.create_task(VoiceLoop._watch_for_interruption(loop, state))
        await asyncio.sleep(1.6)                    # well past the capture bound

        check("the capture was entered", loop.captures, 1)
        check("the watcher returned to reading events", loop.reads >= 2, True)
        check("the watcher is still alive and watching", task.done(), False)

        # And it must still obey the shutdown signal, cancel or not.
        state.done = True
        await asyncio.sleep(0.7)
        check("the mic was handed back after the abandoned capture", loop.rearmed >= 1, True)
        check("state.done ends the watcher", task.done(), True)
        if not task.done():
            task.cancel()
    finally:
        config.INTERRUPT_CAPTURE_SECONDS = original

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("watcher lifetime checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
