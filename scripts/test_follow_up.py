#!/usr/bin/env python3
"""Follow-up mode: the device keeps listening after it answers.

The request, 2026-09-22: "enable a new control mode 'turn on/off follow up mode', which would
cause the device to immediately listen for responses for about 15 seconds on its own after talking
to it when enabled".

What is checked here, with no device, no mic and no network:

  * the spoken controls turn the mode on and off, persist it, and answer out loud;
  * an ordinary question is NOT intercepted as a control;
  * the follow-up window captures an utterance without a wake word;
  * a silent window closes (and does not hold the mic or the announcement gate);
  * the window holds the active-turn flag, so an announcement cannot be captured and answered;
  * a chain of follow-ups is bounded, stops on silence, and stops the moment the mode is turned off;
  * with the mode off, nothing changes about how the device behaves.

    .venv/bin/python scripts/test_follow_up.py
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gateway.config as config_module  # noqa: E402
import gateway.loop as loop_module  # noqa: E402
from gateway.endpointing import Endpointer  # noqa: E402
from gateway.endpointing import EndpointResult  # noqa: E402
from gateway.satellite import SatelliteClient  # noqa: E402

PROBLEMS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PROBLEMS
    print(f"   {'OK  ' if ok else 'FAIL'} {label}"
          f"{(' -- ' + str(detail)) if detail else ''}")
    if not ok:
        PROBLEMS += 1


def utterance(seconds: float = 1.0) -> EndpointResult:
    return EndpointResult(audio=b"\0" * 32000, rate=16000, duration=seconds,
                          speech_seconds=seconds, first_speech_at=0.2,
                          speech_absolute_at=0.2, ended_by="silence", rejected=False)


def blip(speech_seconds: float = 0.08) -> EndpointResult:
    """What the device's own tone looks like when it leaks into the mic: a few frames of noise."""
    return EndpointResult(audio=b"\0" * 8960, rate=16000, duration=0.28,
                          speech_seconds=speech_seconds, first_speech_at=0.0,
                          speech_absolute_at=0.0, ended_by="silence", rejected=True)


def silent_window() -> EndpointResult:
    return EndpointResult(audio=b"", rate=16000, duration=0.0, speech_seconds=0.0,
                          first_speech_at=0.0, speech_absolute_at=0.0,
                          ended_by="no_speech", rejected=True)


def bare_loop():
    """A VoiceLoop with no satellite, no gateway and no endpointer: only what these paths touch."""
    loop = object.__new__(loop_module.VoiceLoop)
    loop.follow_up = False
    loop.stop_requested = False
    loop.turn_started_at = None
    loop.turns = []
    # capture_follow_up scopes the no-speech budget on the endpointer, so the stub needs one.
    loop._ep = Endpointer(silence_ms=700, min_speech_ms=250)
    # A production-backed satellite stub that counts re-arm requests. RunSatellite stops the
    # stream and puts the satellite back to wake-word detection, which a closed window must always do.
    rearm = {"calls": 0}

    class FakeSat(SatelliteClient):
        def __init__(self):
            super().__init__("127.0.0.1")

        async def run(self):
            rearm["calls"] += 1

    loop.sat = FakeSat()
    loop.rearm = rearm
    return loop


async def main() -> int:
    tmp = Path("/tmp/follow-up-test-state")
    tmp.unlink(missing_ok=True)
    config_module.FOLLOW_UP_STATE_FILE = tmp
    original_settle = config_module.FOLLOW_UP_SETTLE_SECONDS
    config_module.FOLLOW_UP_SETTLE_SECONDS = 0.0     # no real waiting in tests

    print("=== FOLLOW-UP MODE (spoken controls) ===")

    # 1. Spoken on/off, with persistence.
    loop = bare_loop()
    reply_on = loop._local_command("Turn on follow up mode.")
    check("spoken 'turn on' is handled and answers", "on" in reply_on.lower(), repr(reply_on))
    check("the mode is now on", loop.follow_up is True)
    check("it persisted to the state file", tmp.read_text().strip() == "on",
          repr(tmp.read_text()))

    reply_off = loop._local_command("turn off follow up mode")
    check("spoken 'turn off' turns it off", loop.follow_up is False and "off" in reply_off.lower(),
          repr(reply_off))
    check("the off state persisted too", tmp.read_text().strip() == "off")

    # 2. It survives a restart, which is the point of persisting it.
    loop._local_command("follow up mode on")
    fresh = bare_loop()
    check("a fresh process reads the flag back on", fresh._load_follow_up() is True)

    # 2b. Spoken "stop" ends the conversation without switching the mode off (the 2026-09-22 request:
    # "ok nabu stop should be a way to stop the current follow up conversation, but leave it open to
    # retrigger again").
    loop.follow_up = True
    loop.stop_requested = False
    reply = loop._local_command("Ok Nabu stop")
    check("a spoken stop is handled", reply == "Stopping.", repr(reply))
    check("...it asks the chain to end", loop.stop_requested is True)
    check("...and leaves follow-up mode ON, so the next conversation follows up again",
          loop.follow_up is True)
    loop.stop_requested = False
    variants = ("stop", "Stop.", "that's all", "never mind", "we're done", "goodbye",
                # These are the forms actually used on 2026-09-22, and the first one is the
                # regression: "Okay, stop." normalises with a double space where the comma was.
                "Okay, stop.", "Stop listening to me!", "Okay, now will you stop?",
                "will you stop", "be quiet")
    for phrase in variants:
        loop.stop_requested = False
        if loop._local_command(phrase) != "Stopping.":
            check(f"all the stop variants work: {phrase!r}", False)
            break
    else:
        check(f"all the stop variants work ({len(variants)} forms)", True)
        check("...and none of them touched the mode", loop.follow_up is True)

    # 2c. "stop" is an ordinary word: only a whole-utterance stop may be intercepted.
    from gateway.config import STOP_CORES
    loop.stop_requested = False
    for sentence in ("stop the sprinklers", "don't stop the music",
                     "remind me where the bus stop is", "stop by the store later",
                     "please stop the dishwasher at nine", "can you stop by and feed the cat"):
        if loop._local_command(sentence):
            check(f"a sentence containing stop is NOT a stop: {sentence!r}", False)
            break
    else:
        check("sentences containing 'stop' are left for the model (4 forms)", True)
    check("...and none of them set the stop flag", loop.stop_requested is False)
    check("plain 'stop' is a recognised core", "stop" in STOP_CORES)
    check("a lead-in plus a core is recognised ('will you stop')",
          loop._local_command("will you stop") == "Stopping.")

    # 3. Status, and no false interception. A question must not change the mode: the status phrase
    # "is follow up mode on" contains the on phrase, which really did turn it on in the first build.
    status = loop._local_command("is follow up mode on")
    check("asking status gives the short status line",
          status == "Follow-up mode is on.", repr(status))
    check("...and did not turn it on with the long confirmation", loop.follow_up is True)
    loop.set_follow_up(False)
    status_off = loop._local_command("is follow up mode on")
    check("asking status from off leaves it OFF and says so",
          status_off == "Follow-up mode is off." and loop.follow_up is False, repr(status_off))
    loop.set_follow_up(True)
    for phrase in ("what is the temperature outside", "turn on the porch light",
                   "set the fairy lights to blue", "follow the money"):
        if loop._local_command(phrase):
            check(f"ordinary speech is not a control: {phrase!r}", False)
            break
    else:
        check("ordinary speech is never intercepted (4 phrases)", True)
    check("...and none of them changed the mode", loop.follow_up is True)

    print("\n=== THE LISTENING WINDOW ===")

    # 4. An utterance with no wake word.
    loop = bare_loop()
    calls = []

    async def capture_utterance(wake_word, _script=[utterance(), silent_window()]):
        calls.append(wake_word)
        return _script.pop(0) if _script else silent_window()

    loop.capture_utterance = capture_utterance
    endpoint, note = await loop.capture_follow_up()
    check("it captured the reply without a wake word",
          endpoint is not None and not endpoint.rejected and calls == ["(follow-up)"])
    check("the announcement gate is held while listening", loop.turn_started_at is not None)

    # 5. A silent window ends it, releases the mic and releases the announcement gate.
    loop = bare_loop()
    calls = []

    async def silent_capture(wake_word):
        calls.append(wake_word)
        return silent_window()

    loop.capture_utterance = silent_capture
    endpoint, note = await loop.capture_follow_up()
    check("silence closes the window", endpoint is None and "nothing said" in note, repr(note))
    check("the announcement gate is released", loop.turn_started_at is None)

    # 5b. A blip is noise, not a reply: it must not close the window (2026-09-22, after
    # the device's own done tone came through as "beep" and consumed the window).
    loop = bare_loop()
    script = [blip(), blip(0.12), utterance()]
    seen = []

    async def blip_then_speech(wake_word):
        seen.append(wake_word)
        return script.pop(0) if script else silent_window()

    loop.capture_utterance = blip_then_speech
    endpoint, note = await loop.capture_follow_up()
    check("blips during follow-up do not close the window",
          endpoint is not None and not endpoint.rejected and len(seen) == 3,
          f"captures={len(seen)}")
    check("a window that hands over to a turn does NOT re-arm (the turn closes the stream itself)",
          loop.rearm["calls"] == 0, f"re-arm calls={loop.rearm['calls']}")

    # 5c. The window shows itself on the lights, then goes dark, so it cannot look switched off.
    loop = bare_loop()
    events = []

    async def fake_publish(event_type):
        events.append(event_type)

    loop.publish_led = fake_publish

    async def silence(wake_word):
        return silent_window()

    loop.capture_utterance = silence
    await loop.capture_follow_up()
    # It must ask for the microphone first (the satellite will not stream otherwise), show that it is
    # listening, and go dark when the window closes.
    check("the window asks for the mic, shows led-followup, then goes dark",
          events == ["wake-now", "led-followup", "led-idle"], events)
    # And it must hand the satellite back to wake-word detection, or the microphone stays open, the
    # lights stay yellow and the wake word is not being listened for.
    check("a window that hears nothing re-arms the wake word",
          loop.rearm["calls"] == 1, f"re-arm calls={loop.rearm['calls']}")

    # 5c-bis. The microphone is asked for twice, because the request is occasionally not acted on.
    loop = bare_loop()
    events = []
    original_quiet = config_module.FOLLOW_UP_QUIET_SECONDS
    config_module.FOLLOW_UP_QUIET_SECONDS = 0.0

    async def fake_publish2(event_type):
        events.append(event_type)

    loop.publish_led = fake_publish2

    async def silence2(wake_word):
        await asyncio.sleep(0.05)
        return silent_window()

    loop.capture_utterance = silence2
    original_retry = config_module.FOLLOW_UP_WAKE_RETRY_SECONDS
    config_module.FOLLOW_UP_WAKE_RETRY_SECONDS = 0.05

    async def keep_listening(wake_word):
        await asyncio.sleep(0.4)
        return silent_window()

    loop.capture_utterance = keep_listening
    await loop.capture_follow_up()
    wakes = [e for e in events if e == "wake-now"]
    check("the window asks for the microphone twice", len(wakes) == 2, events)
    check("...and still ends with the lights dark", events[-1] == "led-idle", events)

    # A window that hands over quickly must not leave a retry pending.
    loop = bare_loop()
    events = []
    loop.publish_led = fake_publish2

    async def immediate(wake_word):
        return utterance()

    loop.capture_utterance = immediate
    await loop.capture_follow_up()
    await asyncio.sleep(0.15)
    wakes = [e for e in events if e == "wake-now"]
    check("a hand-over cancels the pending retry", len(wakes) == 1, events)
    config_module.FOLLOW_UP_WAKE_RETRY_SECONDS = original_retry
    config_module.FOLLOW_UP_QUIET_SECONDS = original_quiet

    # 5c-ter. The window must not listen while its own wake chime is still ringing, or the device
    # answers its own boop (2026-09-22). The endpointer must not be fed before the quiet
    # period has passed.
    loop = bare_loop()
    started = {"at": 0.0}
    fed = {"at": None}

    async def fake_publish3(event_type):
        pass

    loop.publish_led = fake_publish3

    async def note_first_feed(wake_word):
        fed["at"] = asyncio.get_running_loop().time() - started["at"]
        return silent_window()

    loop.capture_utterance = note_first_feed
    config_module.FOLLOW_UP_QUIET_SECONDS = 0.3
    started["at"] = asyncio.get_running_loop().time()
    await loop.capture_follow_up()
    check("the window stays deaf while its own chime rings",
          fed["at"] is not None and fed["at"] >= 0.3,
          f"first audio evaluated at +{fed['at']:.2f}s")
    config_module.FOLLOW_UP_QUIET_SECONDS = original_quiet

    # 5d. Endless blips cannot hold the window open past its deadline.
    keep_seconds = config_module.FOLLOW_UP_SECONDS
    config_module.FOLLOW_UP_SECONDS = 0.2
    loop = bare_loop()
    events = []
    loop.publish_led = fake_publish

    async def always_blip(wake_word):
        await asyncio.sleep(0.05)
        return blip()

    loop.capture_utterance = always_blip
    endpoint, note = await loop.capture_follow_up()
    check("blips still cannot hold the window past FOLLOW_UP_SECONDS",
          endpoint is None and "of follow-up" in note, repr(note))
    config_module.FOLLOW_UP_SECONDS = keep_seconds

    # 6. The window is bounded by FOLLOW_UP_SECONDS even if the capture never returns.
    loop = bare_loop()

    async def never_returns(wake_word):
        await asyncio.sleep(30)

    loop.capture_utterance = never_returns
    original_seconds = config_module.FOLLOW_UP_SECONDS
    config_module.FOLLOW_UP_SECONDS = 0.2
    endpoint, note = await loop.capture_follow_up()
    check("a stuck capture cannot hold the mic forever", endpoint is None and "0s of follow-up" in note,
          repr(note))
    config_module.FOLLOW_UP_SECONDS = original_seconds

    print("\n=== THE CHAIN ===")

    # 7. Two replies, then silence: the chain stops on its own.
    loop = bare_loop()
    loop.follow_up = True
    script = [utterance(), utterance(), None]
    seen = []

    async def fake_one_turn(follow_up=False):
        stats = loop_module.TurnStats(session_id="test")
        seen.append(follow_up)
        item = script.pop(0)
        stats.transcript = "yes please" if item is not None else ""
        loop.turn_started_at = None
        return stats

    loop.one_turn = fake_one_turn
    extra = await loop.follow_up_chain()
    check("follow-up turns are marked as such", all(seen), seen)
    # Three windows: one after each of the two replies, then a third that finds silence and closes.
    check("two replies then silence means three windows, the last one empty",
          extra == 3 and len(script) == 0, f"extra={extra} script_left={len(script)}")

    # 8. Bounded: an endless talker cannot hold the device in a chain forever.
    loop = bare_loop()
    loop.follow_up = True
    config_module.FOLLOW_UP_MAX_TURNS = 3
    count = {"n": 0}

    async def endless(follow_up=False):
        count["n"] += 1
        stats = loop_module.TurnStats(session_id="test")
        stats.transcript = "and another thing"
        return stats

    loop.one_turn = endless
    extra = await loop.follow_up_chain()
    check("the chain is capped by FOLLOW_UP_MAX_TURNS",
          extra == 3 and count["n"] == 3, f"extra={extra} calls={count['n']}")

    # 9. Saying "turn off follow up mode" mid-conversation ends it.
    loop = bare_loop()
    loop.follow_up = True

    async def turns_it_off(follow_up=False):
        stats = loop_module.TurnStats(session_id="test")
        stats.transcript = "turn off follow up mode"
        loop.set_follow_up(False)          # what the spoken control does
        return stats

    loop.one_turn = turns_it_off
    extra = await loop.follow_up_chain()
    check("a spoken turn-off ends the chain", extra == 1 and loop.follow_up is False,
          f"extra={extra}")

    # 9b. A spoken stop ends the chain and re-arms the wake word, with the mode left on.
    loop = bare_loop()
    loop.follow_up = True
    loop.stop_requested = False

    async def says_stop(follow_up=False):
        stats = loop_module.TurnStats(session_id="test")
        stats.transcript = "stop"
        loop.stop_requested = True          # what the spoken control does
        return stats

    loop.one_turn = says_stop
    extra = await loop.follow_up_chain()
    check("a spoken stop ends the chain", extra == 1, f"extra={extra}")
    check("...the wake word is re-armed so the device still works",
          loop.rearm["calls"] == 1, f"re-arm calls={loop.rearm['calls']}")
    check("...the stop flag is consumed, not left set", loop.stop_requested is False)
    check("...and follow-up mode is still ON", loop.follow_up is True)

    # 10. Mode off means nothing changes at all.
    loop = bare_loop()
    loop.follow_up = False

    async def must_not_run(follow_up=False):
        raise AssertionError("the chain called one_turn while follow-up mode was off")

    loop.one_turn = must_not_run
    extra = await loop.follow_up_chain()
    check("with the mode off the chain does nothing", extra == 0)

    print("\n=== THE TURN PATH ===")

    # 11. one_turn(follow_up=True) never waits for a wake word.
    loop = bare_loop()
    loop.gateway = types.SimpleNamespace(session_id="test")
    waited = {"n": 0}

    async def no_wake(timeout=3600.0):
        waited["n"] += 1
        return "okay_nabu"

    async def no_capture():
        return None, "nothing said during follow-up"

    loop.wait_for_wake = no_wake
    loop.capture_follow_up = no_capture
    stats = await loop.one_turn(follow_up=True)
    check("a follow-up turn does not wait for a wake word", waited["n"] == 0, f"waits={waited['n']}")
    check("an empty follow-up window returns quietly",
          stats is not None and stats.transcript == "" and "nothing said" in stats.note,
          repr(stats.note if stats else None))

    config_module.FOLLOW_UP_SETTLE_SECONDS = original_settle
    tmp.unlink(missing_ok=True)
    print(f"\n=== {'ALL CHECKS PASSED' if PROBLEMS == 0 else f'{PROBLEMS} PROBLEM(S)'} ===")
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
