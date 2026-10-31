#!/usr/bin/env python3
"""A stop heard mid-task must set stop_playback BEFORE it speaks (issue #15).

The handler decides and returns; the watcher sets the flag and then speaks. Speaking first would
block on the playback lock held by the very answer being interrupted, up to the 30 second acquire
timeout, with the audio still playing. Run with the Gateway venv:

    .venv/bin/python scripts/test_stop_ordering.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import traceback
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import gateway.loop as loop_module
from gateway.announce import Announcer
from wyoming.event import Event

Bridge = next(obj for obj in vars(loop_module).values()
              if isinstance(obj, type) and hasattr(obj, "_watch_for_interruption"))

checks = 0
failures = []


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(label + ": got " + repr(got) + ", wanted " + repr(want))


class StubGateway:
    async def stop(self):
        return True


async def main():
    global checks
    gateway = Bridge.__new__(Bridge)
    gateway.gateway = StubGateway()
    gateway.interrupt_enabled = True
    gateway.stop_requested = False
    gateway.sat = types.SimpleNamespace(eof=False)
    gateway.self_trigger_count = 0

    order = []
    state = types.SimpleNamespace(done=False, stop_playback=False)

    def local_command(text):
        gateway.stop_requested = True
        return ""

    async def transcribe(audio):
        return "stop"

    async def capture_utterance(wake):
        return types.SimpleNamespace(audio=b"", rejected=False, ended_by="", speech_seconds=1.0)

    async def end_capture():
        order.append("capture ended")

    async def say(text):
        # record what the flag was AT THE MOMENT the words were spoken: that is the whole check
        order.append(("spoke", text, state.stop_playback))

    fed = {"n": 0}

    async def next_event(timeout):
        fed["n"] += 1
        if fed["n"] == 1:
            return Event(type="detection", data={"name": "hey_hermes"})
        await asyncio.sleep(0.05)
        return None

    gateway._local_command = local_command
    gateway.transcribe = transcribe
    gateway.capture_utterance = capture_utterance
    gateway._end_capture = end_capture
    gateway.say = say
    # The watcher owns the direct satellite read, not the shared replay helper (#82).
    gateway.sat.next_event = next_event

    error = None
    task = asyncio.create_task(gateway._watch_for_interruption(state))
    for _ in range(80):
        await asyncio.sleep(0.02)
        if state.stop_playback:
            break
    state.done = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        error = traceback.format_exc()

    if error:
        print("the watcher raised:")
        print(error)
        failures.append("watcher raised: " + error.strip().splitlines()[-1][:120])
        checks += 1
    check("stop_playback was set", state.stop_playback, True)
    spoke = [o for o in order if isinstance(o, tuple)]
    check("the acknowledgement was spoken", bool(spoke), True)
    if spoke:
        check("flag was already set when it spoke", spoke[0][2], True)
        check("and it was the right words", spoke[0][1], "Stopped.")
    check("the capture was ended first", order[:1], ["capture ended"])

    print("stop ordering checks: " + str(checks - len(failures)) + "/" + str(checks) + " passed")
    for f in failures:
        print("  FAIL: " + f)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
