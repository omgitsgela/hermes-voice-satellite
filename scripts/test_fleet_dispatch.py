#!/usr/bin/env python3
"""Fleet restart-class dispatch: pacing under concurrency (review F7), hung-dispatch
containment (second-pass F7), and shutdown cancellation (review F8).

F7: two fleet writes that both touch a restart-class setting must not bounce two rooms at
once. Each device reserves its slot under the pacing lock (a fast section that never awaits
external work), then sleeps and dispatches OUTSIDE it, with the dispatch bounded by a
timeout. A device whose connection stopped consuming output must not hold the fleet's
pacing slot forever and stall every other room's restart-class change.

F8: a fleet dispatch that is still sleeping when the Gateway stops must be cancelled and
awaited, and that cleanup must run even if an earlier shutdown step raises.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import satellite_gateway.announce as announce  # noqa: E402
from satellite_gateway.announce import Announcer  # noqa: E402
from satellite_gateway.config_plane import ConfigPlane  # noqa: E402


class FakeSatellite:
    async def play_wav(self, _path, *, wait_for_played=False):
        return None


problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


def make_announcer(tmp, connected, dispatch):
    plane = ConfigPlane(pathlib.Path(tmp) / "fleet.json")
    announcer = Announcer(
        sat=FakeSatellite(),
        turn_started_at=lambda: None,
        timer_state_path=pathlib.Path(tmp) / "timers.json",
        config_plane=plane,
        connected_device_ids=lambda: connected,
        config_dispatch=dispatch,
    )
    announcer._token = "t"
    return announcer


async def test_pacing_under_concurrency() -> None:
    """F7: concurrent restart-class dispatches space their restarts by the interval."""
    with tempfile.TemporaryDirectory() as tmp:
        order: list[tuple[str, float]] = []
        start = time.monotonic()

        async def dispatch(device_id: str, _record: dict) -> bool:
            order.append((device_id, time.monotonic() - start))
            return True

        ann = make_announcer(tmp, {"a-1", "b-1", "c-1", "d-1"}, dispatch)
        old_interval = announce.FLEET_RESTART_DISPATCH_INTERVAL_SECONDS
        announce.FLEET_RESTART_DISPATCH_INTERVAL_SECONDS = 0.25
        try:
            # Two concurrent fleet writes, each restart-class, four connected devices total.
            records_a = [{"device_id": "a-1"}, {"device_id": "b-1"}]
            records_b = [{"device_id": "c-1"}, {"device_id": "d-1"}]
            await asyncio.gather(
                ann._dispatch_restart_class(records_a),
                ann._dispatch_restart_class(records_b),
            )
        finally:
            announce.FLEET_RESTART_DISPATCH_INTERVAL_SECONDS = old_interval

        check("every device was dispatched exactly once",
              len(order) == 4 and len({d for d, _ in order}) == 4, order)
        # The whole point: no two restarts fire in the same instant. Each device reserves a
        # distinct slot, so the span across four devices is at least three intervals; with a
        # shared deadline they would all fire within a few ms of each other.
        times = sorted(t for _, t in order)
        check("concurrent fleet dispatches are paced, not simultaneous",
              (times[-1] - times[0]) >= 0.4,
              f"span={times[-1] - times[0]:.3f}s over {len(times)} devices")
        gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
        check("each restart was handed its own slot (no two back-to-back)",
              all(g >= 0.15 for g in gaps), f"gaps={[round(g, 3) for g in gaps]}")


async def test_hung_dispatch_does_not_stall_the_fleet() -> None:
    """Second-pass F7: a dispatch that hangs (a connection that stopped consuming output)
    is bounded by the dispatch timeout and must not hold the pacing slot forever."""
    with tempfile.TemporaryDirectory() as tmp:
        order: list[tuple[str, float]] = []
        start = time.monotonic()

        async def dispatch(device_id: str, _record: dict) -> bool:
            if device_id == "hung-1":
                # A write that never completes: the connection's drain() would block.
                await asyncio.sleep(60)
            order.append((device_id, time.monotonic() - start))
            return True

        ann = make_announcer(tmp, {"hung-1", "next-1"}, dispatch)
        old_interval = announce.FLEET_RESTART_DISPATCH_INTERVAL_SECONDS
        old_timeout = announce.FLEET_DISPATCH_TIMEOUT_SECONDS
        announce.FLEET_RESTART_DISPATCH_INTERVAL_SECONDS = 0.2
        announce.FLEET_DISPATCH_TIMEOUT_SECONDS = 0.3
        try:
            task = asyncio.create_task(ann._dispatch_restart_class(
                [{"device_id": "hung-1"}, {"device_id": "next-1"}]), name="fleet-hang")
            # The hung device must not hold the slot: wait (up to a bound) for next-1 to be
            # dispatched while hung-1 is still (un)blocking. With the timeout fix, hung-1's
            # dispatch times out and the loop advances to next-1; without it, next-1 is never
            # reached because the same sequential loop is stuck on hung-1.
            seen_next = None
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                for d, t in order:
                    if d == "next-1":
                        seen_next = t
                        break
                if seen_next is not None or task.done():
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            announce.FLEET_RESTART_DISPATCH_INTERVAL_SECONDS = old_interval
            announce.FLEET_DISPATCH_TIMEOUT_SECONDS = old_timeout
        # next-1 was dispatched while hung-1 is still (un)blocking: the fleet did not wait
        # for the hung device. Without the timeout bound, next-1 is never reached (None).
        check("a hung dispatch does not stall the next device's dispatch",
              seen_next is not None and seen_next < 0.8,
              f"next-1 at {seen_next!r}")


async def test_shutdown_cancels_pending_dispatch() -> None:
    """F8: a still-sleeping fleet dispatch is cancelled and awaited by stop(), even if an
    earlier shutdown step raises."""
    with tempfile.TemporaryDirectory() as tmp:
        dispatched: list[str] = []

        async def dispatch(device_id: str, _record: dict) -> bool:
            dispatched.append(device_id)
            return True

        ann = make_announcer(tmp, {"a-1"}, dispatch)
        old_interval = announce.FLEET_RESTART_DISPATCH_INTERVAL_SECONDS
        announce.FLEET_RESTART_DISPATCH_INTERVAL_SECONDS = 5.0  # long: it will be mid-sleep
        try:
            # A-1 is connected and the pacing deadline is far in the future (a preceding
            # fleet write just claimed the slot), so the task sits in its pacing sleep.
            # Kick it off without awaiting.
            ann._restart_dispatch_at = time.monotonic() + 5.0
            task = asyncio.create_task(
                ann._dispatch_restart_class([{"device_id": "a-1"}]), name="fleet-test")
            ann._fleet_tasks.add(task)
            # Let it reach the sleep.
            await asyncio.sleep(0.05)
            check("the pending dispatch has not yet fired while sleeping",
                  dispatched == [], dispatched)

            # Case 1: a clean stop cancels and drains the pending dispatch.
            await ann.stop()
            check("stop() drains the tracked fleet task set", not ann._fleet_tasks,
                  repr(ann._fleet_tasks))
            check("the cancelled dispatch did not fire after shutdown",
                  dispatched == [], dispatched)

            # Case 2: stop() must STILL clean up the fleet tasks when an earlier shutdown
            # step raises (e.g. the worker already failed) - a task that outlives stop()
            # is a service being restarted after the Gateway is down.
            ann2 = make_announcer(tmp, {"b-1"}, dispatch)
            ann2._restart_dispatch_at = time.monotonic() + 5.0
            task2 = asyncio.create_task(
                ann2._dispatch_restart_class([{"device_id": "b-1"}]), name="fleet-test2")
            ann2._fleet_tasks.add(task2)
            await asyncio.sleep(0.05)

            class Boom:
                async def stop(self):
                    raise RuntimeError("earlier shutdown step failed")

            ann2.timers = Boom()
            raised = False
            try:
                await ann2.stop()
            except RuntimeError as exc:
                raised = "earlier shutdown step failed" in str(exc)
            check("the original shutdown error is preserved", raised)
            check("fleet tasks are drained even when an earlier shutdown step raises",
                  not ann2._fleet_tasks, repr(ann2._fleet_tasks))
            check("the pending dispatch did not fire after a failed shutdown",
                  dispatched == [], dispatched)
        finally:
            announce.FLEET_RESTART_DISPATCH_INTERVAL_SECONDS = old_interval


async def main() -> int:
    await test_pacing_under_concurrency()
    await test_hung_dispatch_does_not_stall_the_fleet()
    await test_shutdown_cancels_pending_dispatch()
    if problems:
        print("FAILURES")
        for p in problems:
            print(" -", p)
        return 1
    print(f"fleet dispatch pacing/shutdown checks: {checks}/{checks} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
