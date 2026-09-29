#!/usr/bin/env python3
"""Fleet restart-class dispatch: pacing under concurrency (review F7) and
shutdown cancellation (review F8).

F7: two fleet writes that both touch a restart-class setting must not bounce two rooms at
once. The wait/dispatch/deadline cycle is one critical section, so each device is handed a
fresh deadline before the next one sleeps.

F8: a fleet dispatch that is still sleeping when the bridge stops must be cancelled and
awaited, not left running - it would otherwise keep restarting device services after the
bridge has shut down.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import bridge.announce as announce  # noqa: E402
from bridge.announce import Announcer  # noqa: E402
from bridge.config_plane import ConfigPlane  # noqa: E402


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
        # The whole point: no two restarts fire in the same instant. With the lock, each
        # device waits its turn; without it, all four would fire within a few ms of each
        # other because they shared one deadline.
        times = sorted(t for _, t in order)
        # The last dispatch must be at least ~1 interval after the first (there are three
        # gaps across four devices), which is impossible if they all fired together.
        check("concurrent fleet dispatches are paced, not simultaneous",
              (times[-1] - times[0]) >= 0.4,
              f"span={times[-1] - times[0]:.3f}s over {len(times)} devices")
        # And no two adjacent dispatches land within an instant of each other.
        gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
        check("each restart is handed a fresh deadline (no two back-to-back)",
              all(g >= 0.15 for g in gaps), f"gaps={[round(g,3) for g in gaps]}")


async def test_shutdown_cancels_pending_dispatch() -> None:
    """F8: a still-sleeping fleet dispatch is cancelled and awaited by stop()."""
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
            await ann.stop()
        finally:
            announce.FLEET_RESTART_DISPATCH_INTERVAL_SECONDS = old_interval
        check("stop() drains the tracked fleet task set", not ann._fleet_tasks,
              repr(ann._fleet_tasks))
        check("the cancelled dispatch did not fire after shutdown",
              dispatched == [], dispatched)


async def main() -> int:
    await test_pacing_under_concurrency()
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
