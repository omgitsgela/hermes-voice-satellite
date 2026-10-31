#!/usr/bin/env python3
"""A rejected announcement must remain retryable (#83), without network or disk."""
import asyncio
import json
import pathlib
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from satellite_gateway.announce import Announcer


class AnnouncementRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_full_does_not_poison_deduplication(self):
        announcer = Announcer.__new__(Announcer)
        announcer._queue = asyncio.Queue(maxsize=1)
        announcer._recent = {}
        announcer.queued = announcer.dropped = 0
        announcer._authorised = lambda request: True
        announcer._log = lambda entry: None
        request = AsyncMock()
        request.json.return_value = {"text": "Check the oven", "key": "oven"}
        announcer._queue.put_nowait(object())

        rejected = await announcer._handle_announce(request)
        self.assertEqual(rejected.status, 503)
        announcer._queue.get_nowait()
        retry = await announcer._handle_announce(request)
        self.assertEqual(retry.status, 202, retry.text)
        self.assertTrue(json.loads(retry.text)["queued"])
        self.assertEqual(announcer._queue.qsize(), 1)
        self.assertEqual(announcer._queue.get_nowait().text, "Check the oven")
        duplicate = await announcer._handle_announce(request)
        self.assertEqual(duplicate.status, 200)
        self.assertTrue(json.loads(duplicate.text)["duplicate"])
        self.assertTrue(announcer._queue.empty())
        self.assertEqual(announcer.queued, 1)


if __name__ == "__main__":
    unittest.main()
