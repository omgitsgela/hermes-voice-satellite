#!/usr/bin/env python3
"""Offline regression checks for host reboot recovery (issue #91)."""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge.gateway import VoiceGatewayError  # noqa: E402
from bridge.loop import VoiceLoop  # noqa: E402


class FakeGateway:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.health_calls = 0
        self.open_calls = 0

    async def open(self) -> None:
        self.open_calls += 1

    async def health(self) -> dict:
        self.health_calls += 1
        if self.health_calls <= self.failures:
            raise VoiceGatewayError("gateway is still booting")
        return {"status": "ok"}


class FakeSatellite:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.run_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def describe(self):
        return None

    async def run(self) -> None:
        self.run_calls += 1


class FakeAnnouncer:
    def __init__(self) -> None:
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1

    def use_connection(self, _satellite) -> None:
        pass


class RebootRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_readiness_retries_in_process(self) -> None:
        loop = VoiceLoop.__new__(VoiceLoop)
        loop.gateway = FakeGateway(failures=2)
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        with patch("bridge.loop.asyncio.sleep", side_effect=fake_sleep):
            health = await loop._wait_for_gateway_ready()

        self.assertEqual(health, {"status": "ok"})
        self.assertEqual(loop.gateway.health_calls, 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    async def test_start_waits_through_transient_health_failure_before_arming(self) -> None:
        loop = VoiceLoop.__new__(VoiceLoop)
        loop.gateway = FakeGateway(failures=1)
        loop.sat = FakeSatellite()
        loop.announcer = FakeAnnouncer()
        loop.registry = None
        loop.registry_server = None
        loop.rotate_if_stale = lambda: None
        loop._model = lambda: None
        loop.say_startup_tone = AsyncMock()
        retry_seen = asyncio.Event()
        allow_retry = asyncio.Event()

        async def blocked_retry(_seconds: float) -> None:
            retry_seen.set()
            await allow_retry.wait()

        with (
            patch("bridge.config.REGISTRY_ENABLED", False),
            patch("bridge.loop.asyncio.sleep", side_effect=blocked_retry),
            patch("bridge.tts.load_voice", return_value=None),
        ):
            start_task = asyncio.create_task(loop.start())
            await asyncio.wait_for(retry_seen.wait(), 0.2)
            self.assertEqual(loop.sat.run_calls, 0, "the microphone must stay unarmed while Gateway is unready")
            allow_retry.set()
            await asyncio.wait_for(start_task, 0.2)

        self.assertEqual(loop.gateway.health_calls, 2)
        self.assertEqual(loop.sat.connect_calls, 1)
        self.assertEqual(loop.sat.run_calls, 1)

    async def test_registry_bind_failure_exits_when_no_fallback_can_work(self) -> None:
        loop = VoiceLoop.__new__(VoiceLoop)
        loop.gateway = FakeGateway()
        loop.sat = FakeSatellite()
        loop.announcer = FakeAnnouncer()
        loop.registry = None
        loop.registry_server = None
        loop.rotate_if_stale = lambda: None
        loop._model = lambda: None
        loop.say_startup_tone = AsyncMock()
        loop._acquire_a_satellite = AsyncMock()
        bind_attempts = 0

        class RecoveringRegistryServer:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            async def start(self) -> None:
                nonlocal bind_attempts
                bind_attempts += 1
                if bind_attempts == 1:
                    raise OSError("Cannot assign requested address")

        with (
            patch("bridge.config.REGISTRY_ENABLED", True),
            patch("bridge.config.DIRECT_DIAL_FALLBACK", False),
            patch("bridge.registry.RegistryServer", RecoveringRegistryServer),
            patch("bridge.registry.load_credential_lookup", return_value=lambda _token: "satellite"),
            patch("bridge.tts.load_voice", return_value=None),
        ):
            with self.assertRaises(OSError):
                await loop.start()
            await loop.start()

        self.assertEqual(bind_attempts, 2)
        self.assertEqual(loop.announcer.start_calls, 1)
        self.assertEqual(loop.sat.run_calls, 1)

    async def test_registry_mode_can_disable_direct_dial_fallback(self) -> None:
        loop = VoiceLoop.__new__(VoiceLoop)
        loop.registry = types.SimpleNamespace(connections=lambda **_kwargs: [])
        loop.sat = FakeSatellite()
        loop._log_events = False

        with (
            patch("bridge.config.DIRECT_DIAL_FALLBACK", False, create=True),
            patch("bridge.config.REGISTRY_WAIT_RETRY_SECONDS", 0.01),
            patch("bridge.config.REGISTRY_LEASE_WAIT_SECONDS", 0.02),
        ):
            task = asyncio.create_task(loop._acquire_a_satellite())
            await asyncio.sleep(0.05)
            self.assertEqual(loop.sat.connect_calls, 0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_enabled_fallback_observes_the_lease_grace_period(self) -> None:
        loop = VoiceLoop.__new__(VoiceLoop)
        loop.registry = types.SimpleNamespace(connections=lambda **_kwargs: [])
        loop.sat = FakeSatellite()
        loop._log_events = False

        with (
            patch("bridge.config.DIRECT_DIAL_FALLBACK", True, create=True),
            patch("bridge.config.REGISTRY_WAIT_RETRY_SECONDS", 0.01),
            patch("bridge.config.REGISTRY_LEASE_WAIT_SECONDS", 0.03),
        ):
            started = time.monotonic()
            await asyncio.wait_for(loop._acquire_a_satellite(), 0.2)
            elapsed = time.monotonic() - started

        self.assertEqual(loop.sat.connect_calls, 1)
        self.assertGreaterEqual(elapsed, 0.02)


if __name__ == "__main__":
    unittest.main(verbosity=2)
