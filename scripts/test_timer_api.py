#!/usr/bin/env python3
"""Offline authenticated timer/config HTTP checks for issue #5."""
from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import sys
import tempfile
import wave

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gateway.announce import Announcer, _concat_wavs  # noqa: E402
from gateway.config import ALARM_CHIME, ANNOUNCE_SOUNDS, SND_FORMAT, TIMER_CHIME  # noqa: E402


class Sat:
    async def play_wav(self, *_args, **_kwargs):
        pass


class Request:
    remote = "127.0.0.1"

    def __init__(self, body=None, token="test-token") -> None:
        self._body = body
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def json(self):
        return self._body


def payload(response) -> dict:
    return json.loads(response.text)


async def main() -> int:
    problems: list[str] = []
    checks = 0

    def check(label: str, condition: bool, detail="") -> None:
        nonlocal checks
        checks += 1
        print(f"  {'ok  ' if condition else 'FAIL'} {label}" + (f" -- {detail}" if detail else ""))
        if not condition:
            problems.append(label)

    with tempfile.TemporaryDirectory() as tmp:
        announcer = Announcer(
            sat=Sat(),
            turn_started_at=lambda: None,
            timer_state_path=pathlib.Path(tmp) / "timers.json",
        )
        announcer._token = "test-token"

        unauth = await announcer._handle_timers_get(Request(token=""))
        check("timer reads require authentication", unauth.status == 401, unauth.status)

        created = await announcer._handle_timers_post(Request({
            "action": "set", "duration": "90 seconds", "label": "check the popcorn",
        }))
        created_body = payload(created)
        check("set accepts a spoken duration", created.status == 201, created_body)
        timer_id = created_body.get("timer", {}).get("id")
        check("set returns an id and 90 seconds", bool(timer_id) and
              created_body["timer"]["duration_seconds"] == 90, created_body)

        listed = await announcer._handle_timers_get(Request())
        listed_body = payload(listed)
        check("GET lists running timers", listed_body.get("count") == 1, listed_body)
        check("GET reports remaining seconds", listed_body["timers"][0]["remaining_seconds"] <= 90,
              listed_body)

        cancelled = await announcer._handle_timers_post(Request({
            "action": "cancel", "label": "POPCORN",
        }))
        check("cancel by case-insensitive label succeeds", payload(cancelled).get("cancelled") == 1,
              payload(cancelled))

        bad = await announcer._handle_timers_post(Request({"action": "set", "seconds": 0}))
        check("invalid duration is a 400", bad.status == 400, payload(bad))
        unknown = await announcer._handle_timers_post(Request({"action": "explode"}))
        check("unknown action is a 400", unknown.status == 400, payload(unknown))

        await announcer._timer_expired({"id": "t123", "label": "check the popcorn"})
        queued = announcer._queue.get_nowait()
        check("named expiry uses requested wording",
              queued.text == "Your check the popcorn timer is done.", queued.text)
        check("timer expiry selects the timer sound", queued.sound == "timer", queued.sound)

        check("timer sound exists", TIMER_CHIME.is_file(), TIMER_CHIME)
        check("alarm sound exists", ALARM_CHIME.is_file(), ALARM_CHIME)
        if TIMER_CHIME.is_file() and ALARM_CHIME.is_file():
            check("timer and alarm sounds are audibly distinct assets",
                  hashlib.sha256(TIMER_CHIME.read_bytes()).digest() !=
                  hashlib.sha256(ALARM_CHIME.read_bytes()).digest())

        # Existence is not enough. _concat_wavs RAISES on a format mismatch, so a tone in the wrong
        # format would make every timer and alarm announcement fail at run time while every unit test
        # still passed. Exercise the real helper against the real files.
        # NOTE: import these at module scope, never inside this function. A function-local `import
        # tempfile` makes tempfile local for the WHOLE function and shadows the module-level import the
        # test body already depends on, so the test dies with UnboundLocalError before reaching here.
        target = (SND_FORMAT[0], SND_FORMAT[1], SND_FORMAT[2])
        with tempfile.TemporaryDirectory() as tmp:
            speech = pathlib.Path(tmp) / "speech.wav"
            with wave.open(str(speech), "wb") as handle:
                handle.setnchannels(target[2])
                handle.setsampwidth(target[1])
                handle.setframerate(target[0])
                handle.writeframes(b"\x00" * target[0] * target[1] * target[2])

            for name, path in ANNOUNCE_SOUNDS.items():
                if path is None:
                    continue
                with wave.open(str(path)) as handle:
                    actual = (handle.getframerate(), handle.getsampwidth(), handle.getnchannels())
                check(f"{name} tone matches the satellite playback format",
                      actual == target, f"{path.name} is {actual}, playback expects {target}")
                out = pathlib.Path(tmp) / f"out-{name}.wav"
                try:
                    _concat_wavs([path, speech], out)
                except Exception as exc:  # noqa: BLE001 - a mismatch here is the defect under test
                    check(f"{name} tone concatenates with speech", False, f"{type(exc).__name__}: {exc}")
                else:
                    check(f"{name} tone concatenates with speech", out.stat().st_size > 0, out)

    print(f"\ntimer API checks: {checks - len(problems)}/{checks} passed")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
