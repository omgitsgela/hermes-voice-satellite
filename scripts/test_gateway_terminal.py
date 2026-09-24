#!/usr/bin/env python3
"""Offline regression checks for terminal Runs events (issue #52)."""
from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.gateway import VoiceGateway, VoiceGatewayError  # noqa: E402


class Response:
    status = 201

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, content_type=None):
        return {"run_id": "run-test"}


class Session:
    def post(self, *_args, **_kwargs):
        return Response()


async def events(items):
    for item in items:
        yield item


async def collect(items):
    gateway = VoiceGateway(base_url="http://example", api_key="test", session_id="test")
    gateway._session = Session()
    gateway._events = lambda _run_id: events(items)
    return [delta async for delta in gateway.ask("hello")]


async def main():
    checks = 0
    failures = []

    for label, supplied in (
        ("empty EOF", []),
        ("partial answer EOF", [{"event": "message.delta", "delta": "partial"}]),
    ):
        checks += 1
        try:
            await collect(supplied)
        except VoiceGatewayError as exc:
            if "without a terminal event" not in str(exc):
                failures.append(f"{label}: wrong error: {exc}")
        else:
            failures.append(f"{label}: returned normally")

    checks += 1
    completed = await collect([
        {"event": "message.delta", "delta": "complete"},
        {"event": "run.completed"},
    ])
    if completed != ["complete"]:
        failures.append(f"completed run: got {completed!r}")

    print(f"gateway terminal-event checks: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print("  FAIL:", failure)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
