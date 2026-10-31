"""Offline tests for voice interruption. No device, no gateway, no model, no side effects.

2026-09-23: while the device is working on a task, a wake word plus a new utterance
should act as a conversation interruption. Two outcomes matter:

* new information ("it was a joke, that light change was a prank") must reach the agent that is
  already running, not start a fresh question;
* a stop ("stop", "never mind") must actually stop the running agent, which is what failed in the
  original incident: the task kept working in the background.

The third case is the safety one: a wake detection with nothing intelligible after it is our own
audio leaking into the mic, and it must be discarded rather than steered into a live run.

Run: ./.venv/bin/python scripts/test_interrupt.py
"""

from __future__ import annotations

import asyncio
import sys

from wyoming.wake import Detection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from satellite_gateway.loop import VoiceLoop  # noqa: E402

FAILURES: list[str] = []


class StubGateway:
    """Records what the interruption tried to do to the running agent."""

    def __init__(self, *, steer_accepts: bool = True) -> None:
        self.last_run_id = "run-123"
        self.steers: list[str] = []
        self.stops: list[str] = []
        self.steer_accepts = steer_accepts

    async def steer(self, text: str, run_id: str | None = None) -> bool:
        self.steers.append(text)
        return self.steer_accepts

    async def stop(self, run_id: str | None = None) -> bool:
        self.stops.append(run_id or self.last_run_id or "")
        return True


class StubLoop:
    """Only the attributes the interruption path is allowed to touch.

    Note there is no microphone, no satellite and no state file here. The real
    `_local_command` is borrowed, because classifying a stop correctly is exactly what is being
    tested, but a control phrase that would write real state is swapped out in its own case below.
    """

    def __init__(self, *, gateway: StubGateway, enabled: bool = True) -> None:
        self.gateway = gateway
        self.interrupt_enabled = enabled
        self.stop_requested = False
        # The real matcher reads this when it considers follow-up phrases. It is never flipped
        # here, so no test can write real follow-up state.
        self.follow_up = False
        self.said: list[str] = []

    _local_command = VoiceLoop._local_command

    async def say(self, text: str) -> None:
        self.said.append(text)


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


async def main() -> int:
    print("interruption classification (what a mid-task utterance should do)")

    # 1. New information reaches the running agent.
    gw = StubGateway()
    loop = StubLoop(gateway=gw)
    outcome = await VoiceLoop._handle_interruption(loop, "it was a joke, that was a prank")
    check("new information -> steered", outcome, "steered")
    check("  text passed through", gw.steers, ["it was a joke, that was a prank"])
    check("  nothing was stopped", gw.stops, [])

    # 2. A stop stops the run. This is the incident case.
    gw = StubGateway()
    loop = StubLoop(gateway=gw)
    outcome = await VoiceLoop._handle_interruption(loop, "stop")
    check("stop -> stopped", outcome, "stopped")
    check("  stop called on the run", gw.stops, ["run-123"])
    check("  no steer", gw.steers, [])
    # The handler decides; the WATCHER speaks, and only after it has set stop_playback. Speaking
    # from inside the handler would block on the playback lock held by the answer being stopped, up
    # to the 30 second acquire timeout, with the audio still playing (issue #15). So an empty
    # acknowledgement here is the contract, not a regression.
    check("  no acknowledgement from the handler (the caller speaks, issue #15)", loop.said, [])

    # 3. Other whole-utterance stop forms behave the same, via the existing strict matcher.
    for phrase in ("okay stop", "stop it", "stop now", "please stop", "just stop",
                   "stop talking to me"):
        gw = StubGateway()
        loop = StubLoop(gateway=gw)
        outcome = await VoiceLoop._handle_interruption(loop, phrase)
        check(f"'{phrase}' -> stopped", outcome, "stopped")
        check("  which stopped the run", gw.stops, ["run-123"])

    # 4. A sentence that merely contains "stop" is information for the agent, not a stop.
    for phrase in ("stop the sprinklers tomorrow", "i will stop by later",
                   "can you stop the timer"):
        gw = StubGateway()
        loop = StubLoop(gateway=gw)
        outcome = await VoiceLoop._handle_interruption(loop, phrase)
        check(f"'{phrase}' -> steered", outcome, "steered")
        check("  nothing stopped", gw.stops, [])

    # 5. The safety case: our own audio leaking back, with nothing intelligible after the wake.
    for garbage in ("", "   ", "you", "beep", "the"):
        gw = StubGateway()
        loop = StubLoop(gateway=gw)
        outcome = await VoiceLoop._handle_interruption(loop, garbage)
        check(f"{garbage!r} -> ignored", outcome, "ignored")
        check("  no steer on garbage", gw.steers, [])
        check("  no stop on garbage", gw.stops, [])

    # 6. Disabled by config: nothing happens at all.
    gw = StubGateway()
    loop = StubLoop(gateway=gw, enabled=False)
    outcome = await VoiceLoop._handle_interruption(loop, "it was a joke")
    check("disabled -> ignored", outcome, "ignored")
    check("  nothing steered", gw.steers, [])

    # 7. A local control reply that is not a stop is answered locally, not steered. The matcher is
    #    swapped here so this test cannot touch real state (follow-up mode lives in a state file).
    gw = StubGateway()
    loop = StubLoop(gateway=gw)
    loop._local_command = lambda _text: "Follow-up mode is on."   # type: ignore[method-assign]
    outcome = await VoiceLoop._handle_interruption(loop, "turn on follow up mode")
    check("control phrase -> control", outcome, "control")
    check("  not steered", gw.steers, [])
    check("  answered locally", loop.said, ["Follow-up mode is on."])

    # 8. If the server refuses the steer, we must not claim it landed.
    gw = StubGateway(steer_accepts=False)
    loop = StubLoop(gateway=gw)
    outcome = await VoiceLoop._handle_interruption(loop, "actually check the kitchen instead")
    check("refused steer -> steer_failed", outcome, "steer_failed")
    check("  it did try", gw.steers, ["actually check the kitchen instead"])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all interruption checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
