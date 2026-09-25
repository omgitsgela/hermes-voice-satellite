#!/usr/bin/env python3
"""Authenticated HTTP contract checks for the configuration plane (#6/#11)."""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge.announce import Announcer  # noqa: E402
from bridge.config_plane import ConfigPlane  # noqa: E402


class FakeRequest:
    def __init__(self, body=None, *, authorised=True, device_id="kitchen-1") -> None:
        self._body = body
        self.headers = {"Authorization": "Bearer test-token"} if authorised else {}
        self.remote = "127.0.0.1"
        self.match_info = {"device_id": device_id}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSatellite:
    async def play_wav(self, _path, *, wait_for_played=False):
        return None


async def main() -> int:
    problems: list[str] = []
    checks = 0

    def check(label: str, condition: bool, detail="") -> None:
        nonlocal checks
        checks += 1
        print(f"{'PASS' if condition else 'FAIL'} {label}")
        if not condition:
            problems.append(f"{label}: {detail}")

    with tempfile.TemporaryDirectory() as tmp:
        dispatched: list[tuple[str, dict]] = []

        async def dispatch(device_id: str, record: dict) -> bool:
            dispatched.append((device_id, record))
            return True

        plane = ConfigPlane(pathlib.Path(tmp) / "fleet.json")
        announcer = Announcer(
            sat=FakeSatellite(),
            turn_started_at=lambda: None,
            timer_state_path=pathlib.Path(tmp) / "timers.json",
            config_plane=plane,
            connected_device_ids=lambda: {"kitchen-1"},
            config_dispatch=dispatch,
        )
        announcer._token = "test-token"

        unauthorized = await announcer._handle_config_schema(FakeRequest(authorised=False))
        check("config API requires bearer auth", unauthorized.status == 401, unauthorized.text)

        schema_response = await announcer._handle_config_schema(FakeRequest())
        schema = json.loads(schema_response.text)
        check("schema endpoint is versioned and typed",
              schema_response.status == 200 and schema["schema_version"] == 1
              and schema["device_settings"]["mic_gain"]["type"] == "integer", schema)

        write = await announcer._handle_config_desired(FakeRequest({
            "expected_generation": 0,
            "settings": {"wake_word": "hey_hermes", "mic_gain": 78},
        }))
        written = json.loads(write.text)
        check("desired state write returns accepted generation",
              write.status == 202 and written["device"]["generation"] == 1, written)
        check("connected device receives desired state",
              written["dispatched"] is True and dispatched[-1][0] == "kitchen-1", dispatched)

        conflict = await announcer._handle_config_desired(FakeRequest({
            "expected_generation": 0,
            "settings": {"mic_gain": 79},
        }))
        check("stale HTTP write is a conflict", conflict.status == 409, conflict.text)

        listing = await announcer._handle_config_devices(FakeRequest())
        listed = json.loads(listing.text)
        check("fleet listing exposes connection and convergence state",
              listed["devices"][0]["connected"] is True
              and listed["devices"][0]["converged"] is False, listed)

        report = await announcer._handle_config_reported(FakeRequest({
            "generation": 1,
            "actual": {"wake_word": "hey_hermes", "mic_gain": 78},
            "health": "healthy",
            "error": None,
        }))
        reported = json.loads(report.text)
        check("reported state can converge",
              report.status == 200 and reported["device"]["converged"] is True, reported)

        one = await announcer._handle_config_device(FakeRequest())
        check("single-device read is available", json.loads(one.text)["device"]["device_id"] == "kitchen-1")

        page = await announcer._handle_config_page(FakeRequest(authorised=False))
        check("configuration page is static and does not embed a credential",
              page.status == 200 and "/config/devices" in page.text
              and "test-token" not in page.text, page.text[:200])

    if problems:
        print("\nFAILURES")
        for problem in problems:
            print(" -", problem)
        return 1
    print(f"\nconfig HTTP checks: {checks}/{checks} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
