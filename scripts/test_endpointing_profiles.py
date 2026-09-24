#!/usr/bin/env python3
"""Offline checks for pause-tolerant endpointing profiles (issue #26)."""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bridge.config as config_module  # noqa: E402
from bridge.announce import Announcer  # noqa: E402
from bridge.endpointing import Endpointer  # noqa: E402
from bridge.loop import VoiceLoop  # noqa: E402


def check(got, wanted, label: str, failures: list[str]) -> None:
    if got != wanted:
        failures.append(f"{label}: got {got!r}, wanted {wanted!r}")


class ScriptedVad:
    def __init__(self, decisions: list[bool]) -> None:
        self.decisions = iter(decisions)

    def is_speech(self, frame: bytes, rate: int) -> bool:
        return next(self.decisions)


def endpoint_for(profile: str):
    decisions = [True] * 20 + [False] * 60 + [True] * 20 + [False] * 90
    kwargs = {"silence_ms": 700}
    kwargs.update(config_module.ENDPOINTING_PROFILES[profile])
    endpoint = Endpointer(**kwargs)
    endpoint._vad = ScriptedVad(decisions)
    # Audible frames for speech, silence for the rest. Digital silence no longer counts as speech at
    # all: the energy gate rejects anything below the room's floor, so a fixture made of zero samples
    # would test that the endpointer correctly hears nothing, which is not what this file is about.
    speech_frame = b"\x11\x22" * endpoint.frame_samples
    quiet_frame = b"\x00\x00" * endpoint.frame_samples
    result = None
    for decision in decisions:
        result = endpoint.process(speech_frame if decision else quiet_frame)
        if result is not None:
            break
    return result


class Request:
    def __init__(self, body=None) -> None:
        self.headers = {"Authorization": "Bearer test-token"}
        self.remote = "127.0.0.1"
        self._body = body

    async def json(self):
        return self._body


async def check_settings_api(loop: VoiceLoop, failures: list[str]) -> None:
    announcer = Announcer(
        sat=loop.sat,
        turn_started_at=lambda: None,
        follow_up_get=lambda: loop.follow_up,
        follow_up_set=loop.set_follow_up,
        endpointing_profile_get=lambda: loop.endpointing_profile,
        endpointing_profile_set=loop.set_endpointing_profile,
    )
    announcer._token = "test-token"

    response = await announcer._handle_settings_get(Request())
    payload = json.loads(response.text)
    check(response.status, 200, "settings GET status", failures)
    check(payload.get("endpointing_profile"), "standard", "settings GET profile", failures)

    response = await announcer._handle_settings_post(
        Request({"endpointing_profile": "pause_tolerant"}))
    payload = json.loads(response.text)
    check(response.status, 200, "settings POST status", failures)
    check(payload.get("endpointing_profile"), "pause_tolerant", "settings POST profile", failures)
    check(loop.endpointing_profile, "pause_tolerant", "settings POST applied", failures)

    response = await announcer._handle_settings_post(Request({"endpointing_profile": "forever"}))
    check(response.status, 400, "unknown settings profile status", failures)
    check(loop.endpointing_profile, "pause_tolerant", "unknown settings profile preserved", failures)


async def check_mid_capture_swap(loop: VoiceLoop, failures: list[str]) -> None:
    """A profile change during an utterance must not restart or truncate it."""
    from wyoming.audio import AudioChunk

    # Baseline explicitly, so the utterance starts under the standard 700 ms rule and the swap below
    # is a genuine change of timing rather than the state a previous check happened to leave behind.
    loop.set_endpointing_profile("standard")
    frame_samples = loop._ep.frame_samples
    speech = b"\x11\x22" * frame_samples
    silence = b"\x00\x00" * frame_samples

    class ContentVad:
        """Deterministic on purpose: webrtcvad on a synthetic tone is not predictable."""

        def is_speech(self, frame: bytes, rate: int) -> bool:
            return frame[:2] == b"\x11\x22"

    script: list = ([AudioChunk(audio=speech, rate=16000, width=2, channels=1).event()] * 20
                    + [AudioChunk(audio=silence, rate=16000, width=2, channels=1).event()] * 40)

    original = loop._ep
    original._vad = ContentVad()
    fed = {"n": 0}

    class Sat:
        eof = False

    async def scripted_next(timeout):
        if fed["n"] >= len(script):
            raise AssertionError("capture kept waiting: the in-flight utterance was disrupted")
        event = script[fed["n"]]
        fed["n"] += 1
        if fed["n"] == 3:
            # Swap profiles mid-utterance, which is what the settings endpoint can do at any time.
            loop.set_endpointing_profile("pause_tolerant")
        return event

    loop.sat = Sat()
    loop._next = scripted_next
    try:
        result = await asyncio.wait_for(loop.capture_utterance("hey_hermes"), timeout=5.0)
    except AssertionError as exc:
        failures.append(f"mid-capture profile swap: {exc}")
        return
    except asyncio.TimeoutError:
        failures.append("mid-capture profile swap: capture never completed")
        return

    check(result is not None, True, "mid-capture swap returns an utterance", failures)
    # 20 speech frames at 20 ms = 0.4 s. Only the ORIGINAL 700 ms silence rule closes this, so a
    # result of 0.4 s proves the utterance was neither restarted nor carried by the new endpointer.
    check(round(result.speech_seconds, 1), 0.4, "mid-capture swap keeps its own timing", failures)
    check(result.ended_by, "silence", "mid-capture swap still ends normally", failures)
    check(loop.endpointing_profile, "pause_tolerant", "mid-capture swap applies for next time",
          failures)
    check(loop._ep is not original, True, "mid-capture swap replaces the endpointer", failures)


def main() -> int:
    failures: list[str] = []

    standard_audio = endpoint_for("standard")
    tolerant_audio = endpoint_for("pause_tolerant")
    check(round(standard_audio.speech_seconds, 1), 0.4,
          "standard profile ends at a 1.2 second pause", failures)
    check(round(tolerant_audio.speech_seconds, 1), 0.8,
          "pause-tolerant profile carries speech across a 1.2 second pause", failures)

    with tempfile.TemporaryDirectory() as tmp:
        state = pathlib.Path(tmp) / "endpointing-profile"
        config_module.ENDPOINTING_PROFILE_STATE_FILE = state

        loop = VoiceLoop(api_key="test", endpointer={"silence_ms": 700})
        check(loop.endpointing_profile, "standard", "default profile", failures)
        check(loop._ep._silence_frames, 35, "standard silence frames", failures)

        result = loop.set_endpointing_profile("pause_tolerant")
        check(result, "pause_tolerant", "setter result", failures)
        check(state.read_text(), "pause_tolerant\n", "persisted profile", failures)
        check(loop._ep._silence_frames, 90, "pause-tolerant silence frames", failures)
        check(loop._ep._max_frames, 1250, "pause-tolerant hard utterance cap", failures)
        check(loop._ep._no_speech_frames, 600, "pause-tolerant no-speech bound", failures)

        restored = VoiceLoop(api_key="test", endpointer={"silence_ms": 700})
        check(restored.endpointing_profile, "pause_tolerant", "restored profile", failures)
        check(restored._ep._silence_frames, 90, "restored silence frames", failures)

        try:
            restored.set_endpointing_profile("unbounded")
        except ValueError:
            pass
        else:
            failures.append("unknown profile was accepted")
        check(restored.endpointing_profile, "pause_tolerant",
              "invalid profile left state unchanged", failures)

        restored.set_endpointing_profile("standard")
        check(restored._ep._silence_frames, 35, "standard restored", failures)
        check(state.read_text(), "standard\n", "standard persisted", failures)
        asyncio.run(check_settings_api(restored, failures))
        asyncio.run(check_mid_capture_swap(restored, failures))

    passed = 26 - len(failures)
    print(f"endpointing profile checks: {passed}/26 passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
