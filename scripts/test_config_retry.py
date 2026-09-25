#!/usr/bin/env python3
"""Offline same-generation reconciliation and durable idempotence (#86)."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pi.config_agent import DeviceConfigAgent
from wyoming.event import Event


def desired(generation=1, volume=60):
    return Event(type="hermes.config.desired", data={
        "schema_version": 1, "device_id": "test-device", "generation": generation,
        "settings": {"playback_volume": volume},
    })


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_redelivery_retries_once_then_is_idempotent_across_restart(self):
        for restart in (False, True):
            with self.subTest(restart=restart), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                calls = []

                def apply(settings):
                    calls.append(dict(settings))
                    if len(calls) == 1:
                        return {}, "error", "temporary hardware failure"
                    return dict(settings), "healthy", None

                agent = DeviceConfigAgent(path, apply=apply)
                first = await agent.apply_event(desired())
                self.assertEqual(first.data["health"], "error")
                self.assertEqual(json.loads(path.read_text())["health"], "error")
                if restart:
                    agent = DeviceConfigAgent(path, apply=apply)
                second = await agent.apply_event(desired())
                self.assertEqual(second.data["health"], "healthy")
                self.assertEqual(second.data["actual"], {"playback_volume": 60})
                self.assertEqual(len(calls), 2)
                agent = DeviceConfigAgent(path, apply=apply)
                self.assertEqual((await agent.apply_event(desired())).data, second.data)
                self.assertEqual(len(calls), 2)

    async def test_staged_settings_reconcile_with_new_adapter_but_not_changed_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            agent = DeviceConfigAgent(path)
            await agent.apply_event(desired(2))
            calls = []

            def apply(settings):
                calls.append(dict(settings))
                return dict(settings), "healthy", None

            agent = DeviceConfigAgent(path, apply=apply)
            await agent.apply_event(desired(1))
            self.assertEqual(calls, [])  # stale state must never roll hardware back
            conflict = await agent.apply_event(desired(2, volume=90))
            self.assertEqual(conflict.data["health"], "error")
            self.assertEqual(calls, [])
            self.assertEqual(agent.state["desired"], {"playback_volume": 60})
            report = await agent.apply_event(desired(2))
            self.assertEqual(report.data["health"], "healthy")
            self.assertEqual(calls, [{"playback_volume": 60}])

    async def test_degraded_but_fully_applied_state_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def apply(settings):
                calls.append(dict(settings))
                return dict(settings), "degraded", "unrelated diagnostic"

            agent = DeviceConfigAgent(Path(tmp) / "config.json", apply=apply)
            await agent.apply_event(desired())
            await agent.apply_event(desired())
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
