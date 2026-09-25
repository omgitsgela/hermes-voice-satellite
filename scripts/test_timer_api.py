#!/usr/bin/env python3
"""Offline authenticated timer/config HTTP checks for issue #5."""
from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge.announce import Announcer  # noqa: E402
from bridge.config import ALARM_CHIME, TIMER_CHIME  # noqa: E402


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

    print(f"\ntimer API checks: {checks - len(problems)}/{checks} passed")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
