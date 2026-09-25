#!/usr/bin/env python3
"""Deterministic offline checks for bridge timers (issue #5)."""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge.timers import TimerLimitError, TimerManager, parse_duration  # noqa: E402


async def main() -> int:
    problems: list[str] = []
    checks = 0

    def check(label: str, condition: bool, detail: object = "") -> None:
        nonlocal checks
        checks += 1
        print(f"  {'ok  ' if condition else 'FAIL'} {label}" + (f" -- {detail}" if detail else ""))
        if not condition:
            problems.append(label)

    expected = {
        "5 minutes": 300,
        "90 seconds": 90,
        "1 hour 30 minutes": 5400,
        "1h30m": 5400,
        "half an hour": 1800,
        "20 mins": 1200,
        "1.5 hours": 5400,
    }
    for phrase, seconds in expected.items():
        check(f"parse {phrase!r}", parse_duration(phrase) == seconds, parse_duration(phrase))
    for bad in ("", "later", "zero minutes", "-2 minutes", "five elephants"):
        try:
            parse_duration(bad)
        except ValueError:
            check(f"refuse {bad!r}", True)
        else:
            check(f"refuse {bad!r}", False)

    now = [1_000.0]
    fired: list[dict] = []

    async def on_expire(timer: dict) -> None:
        fired.append(dict(timer))

    with tempfile.TemporaryDirectory() as tmp:
        state = pathlib.Path(tmp) / "timers.json"
        manager = TimerManager(state_path=state, on_expire=on_expire, now=lambda: now[0], max_timers=2)
        first = manager.set_timer(90, "Check the popcorn")
        second = manager.set_timer(30, "tea")
        check("timers persist mode 600", (state.stat().st_mode & 0o777) == 0o600,
              oct(state.stat().st_mode & 0o777))
        persisted = json.loads(state.read_text())
        check("both timers persisted", len(persisted["timers"]) == 2, persisted)
        try:
            manager.set_timer(10, "too many")
        except TimerLimitError:
            check("concurrent timer cap is enforced", True)
        else:
            check("concurrent timer cap is enforced", False)

        listed = manager.list_timers()
        check("timers list in deadline order", [t["id"] for t in listed] == [second["id"], first["id"]])
        check("remaining seconds are reported", [t["remaining_seconds"] for t in listed] == [30, 90], listed)

        cancelled = manager.cancel_timer(label="POPCORN")
        check("cancel label is case-insensitive and can match the meaningful part",
              [t["id"] for t in cancelled] == [first["id"]], cancelled)
        check("only the intended timer remains", [t["id"] for t in manager.list_timers()] == [second["id"]])

        # Recreate an overdue timer in persisted state, then load it in a fresh manager.
        manager.set_timer(10, "toast")
        now[0] += 40
        fresh = TimerManager(state_path=state, on_expire=on_expire, now=lambda: now[0], max_timers=10)
        count = await fresh.fire_due()
        check("overdue timers fire in deadline order", [t["label"] for t in fired] == ["toast", "tea"], fired)
        check("all overdue timers fired", count == 2, count)
        check("fired timers are removed before a second pass", await fresh.fire_due() == 0)
        check("fired timers are removed from durable state", fresh.list_timers() == [])

        # Overlapping future timers also fire by due_at, not insertion order.
        fired.clear()
        now[0] = 2_000.0
        fresh.set_timer(20, "later")
        fresh.set_timer(5, "sooner")
        now[0] += 30
        await fresh.fire_due()
        check("overlapping timers fire in deadline order",
              [t["label"] for t in fired] == ["sooner", "later"], fired)

        fresh.set_timer(10, "one")
        fresh.set_timer(20, "two")
        cancelled = fresh.cancel_timer(all_timers=True)
        check("cancel all removes every timer", len(cancelled) == 2 and fresh.list_timers() == [])

        # Setting and cancelling are writes too: if the save fails, memory must not
        # disagree with the disk. A phantom timer fires but never survives a restart; a
        # cancelled timer that was only dropped from memory comes back and fires.
        import errno
        from unittest.mock import patch

        durable = pathlib.Path(tmp) / "durable-timers.json"
        shelf = TimerManager(state_path=durable, on_expire=on_expire, now=lambda: now[0], max_timers=10)
        kept = shelf.set_timer(600, "keep me")
        disk_before = durable.read_bytes()

        with patch.object(shelf, "_save", side_effect=OSError(errno.ENOSPC, "disk full")):
            try:
                shelf.set_timer(30, "phantom")
                set_failed = False
            except OSError:
                set_failed = True
        check("a timer that cannot be persisted reports the failure", set_failed)
        check("a failed set leaves no in-memory phantom timer",
              [t["id"] for t in shelf.list_timers()] == [kept["id"]],
              [t["label"] for t in shelf.list_timers()])
        check("a failed set leaves the file untouched", durable.read_bytes() == disk_before)

        with patch.object(shelf, "_save", side_effect=OSError(errno.ENOSPC, "disk full")):
            try:
                shelf.cancel_timer(timer_id=kept["id"])
                cancel_failed = False
            except OSError:
                cancel_failed = True
        check("a cancel that cannot be persisted reports the failure", cancel_failed)
        check("a failed cancel does not drop a timer that is still on disk",
              [t["id"] for t in shelf.list_timers()] == [kept["id"]],
              [t["label"] for t in shelf.list_timers()])
        check("a failed cancel leaves the file untouched", durable.read_bytes() == disk_before)

        recovered = shelf.set_timer(30, "after recovery")
        check("setting a timer works once storage recovers",
              [t["label"] for t in shelf.list_timers()] == ["after recovery", "keep me"],
              [t["label"] for t in shelf.list_timers()])
        check("and the reader of the file agrees",
              [t["label"] for t in TimerManager(
                  state_path=durable, on_expire=on_expire, now=lambda: now[0],
                  max_timers=10).list_timers()] == ["after recovery", "keep me"])
        check("a cancel works once storage recovers",
              [t["id"] for t in shelf.cancel_timer(timer_id=recovered["id"])] == [recovered["id"]]
              and [t["id"] for t in shelf.list_timers()] == [kept["id"]],
              [t["label"] for t in shelf.list_timers()])

    print(f"\ntimer checks: {checks - len(problems)}/{checks} passed")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
