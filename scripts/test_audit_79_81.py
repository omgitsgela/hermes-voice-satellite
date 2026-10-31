#!/usr/bin/env python3
"""Offline regression checks for connection cleanup and durable configuration."""
import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wyoming.event import Event
from satellite_gateway.registry import SatelliteConnection


class BrokenWriter:
    def __init__(self):
        self.close_count = 0

    def get_extra_info(self, name, default=None):
        return default

    def write(self, data):
        pass

    def writelines(self, lines):
        pass

    async def drain(self):
        raise BrokenPipeError("injected write failure")

    def close(self):
        self.close_count += 1


class ConnectionCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_send_finishes_cleanup(self):
        writer = BrokenWriter()
        connection = SatelliteConnection("test-device", 1, None, writer)
        waiter = asyncio.create_task(connection.wait_for("played"))
        await asyncio.sleep(0)
        try:
            self.assertFalse(await connection.send(Event(type="ping")))
            await connection.close()
            await connection.close()
            await asyncio.sleep(0)
            self.assertTrue(connection.closed_event.is_set(), "failed send must signal closure")
            self.assertEqual(writer.close_count, 1)
            self.assertTrue(waiter.cancelled(), "typed waiter must be cancelled")
            self.assertFalse(await connection.send(Event(type="ping")))
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


class OutboundRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_eof_interrupts_local_retry(self):
        from unittest.mock import patch
        from scripts.test_outbound_runner import FakeBridge, config_for
        from pi.outbound_satellite import BridgeLink

        class ClosingBridge(FakeBridge):
            async def _handle(self, reader, writer):
                try:
                    await super()._handle(reader, writer)
                except ConnectionError:
                    pass  # The test deliberately disconnects this peer.

        gateway = ClosingBridge()
        await gateway.start()
        link = BridgeLink(config_for(gateway.port, relay=True, local_port=1,
                                     local_retry_seconds=60))
        real_connect = asyncio.open_connection
        disconnected = asyncio.Event()
        states = []

        async def connect(host, port, **kwargs):
            if port == 1:
                gateway.writers[-1].close()
                raise ConnectionRefusedError("local service unavailable")
            return await real_connect(host, port, **kwargs)

        async def on_disconnect(reason):
            states.append((link.connected, link.registered))
            disconnected.set()

        link.on_disconnected = on_disconnect
        with patch("pi.outbound_satellite.asyncio.open_connection", side_effect=connect):
            task = asyncio.create_task(link.run())
            disconnect_waiter = asyncio.create_task(disconnected.wait())
            try:
                done, _ = await asyncio.wait([disconnect_waiter], timeout=0.5)
                self.assertTrue(done, "gateway EOF must interrupt local retries")
                self.assertEqual(states[0], (False, False))
                async with asyncio.timeout(2):
                    while len(gateway.registrations) < 2:
                        await asyncio.sleep(0.01)
            finally:
                await link.stop()
                task.cancel()
                disconnect_waiter.cancel()
                await asyncio.gather(task, disconnect_waiter, return_exceptions=True)
                await gateway.stop()


class ConfigPersistenceTests(unittest.TestCase):
    def test_save_has_no_fallible_permission_change_after_commit(self):
        import tempfile
        import stat
        from unittest.mock import patch
        from satellite_gateway.config_plane import ConfigPlane

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "state.json"
            plane = ConfigPlane(path)
            # The staged inode is already private. A second chmod after replace
            # could report failure after disk has committed the new generation.
            with patch("satellite_gateway.config_plane.os.chmod", side_effect=OSError("chmod unavailable")):
                try:
                    result = plane.set_desired("test-device", {"mic_gain": 50})
                except OSError:
                    self.fail("save must not fail after committing the candidate")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(ConfigPlane(path).get_device("test-device"), result)
            self.assertEqual(plane.get_device("test-device"), result)

    def test_failed_enrollment_save_preserves_device(self):
        import errno
        import tempfile
        from unittest.mock import patch
        from satellite_gateway.config_plane import ConfigPlane

        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                path = pathlib.Path(tmp) / "state.json"
                plane = ConfigPlane(path)
                if existing:
                    plane.ensure_device("test-device", metadata={"name": "Old"})
                before = plane.list_devices()
                disk = path.read_bytes() if path.exists() else None
                with patch.object(plane, "_save", side_effect=OSError(errno.ENOSPC, "disk full")):
                    with self.assertRaises(OSError):
                        plane.ensure_device("test-device", metadata={"name": "New"})
                self.assertEqual(plane.list_devices(), before, "failed save must not publish enrollment")
                self.assertEqual(path.read_bytes() if path.exists() else None, disk)
                result = plane.ensure_device("test-device", metadata={"name": "New"})
                self.assertEqual(result["generation"], 1)
                self.assertEqual(ConfigPlane(path).get_device("test-device"), result)

    def test_failed_desired_save_preserves_generation(self):
        import errno
        import tempfile
        from unittest.mock import patch
        from satellite_gateway.config_plane import ConfigPlane

        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                path = pathlib.Path(tmp) / "state.json"
                plane = ConfigPlane(path)
                if existing:
                    plane.ensure_device("test-device")
                before = plane.list_devices()
                disk = path.read_bytes() if path.exists() else None
                generation = 1 if existing else 0
                with patch.object(plane, "_save", side_effect=OSError(errno.ENOSPC, "disk full")):
                    with self.assertRaises(OSError):
                        plane.set_desired("test-device", {"mic_gain": 50},
                                          expected_generation=generation)
                self.assertEqual(plane.list_devices(), before, "failed save must not publish desired state")
                self.assertEqual(path.read_bytes() if path.exists() else None, disk)
                result = plane.set_desired("test-device", {"mic_gain": 50},
                                           expected_generation=generation)
                self.assertEqual(result["generation"], generation + 1)
                self.assertEqual(ConfigPlane(path).get_device("test-device"), result)


if __name__ == "__main__":
    unittest.main()
