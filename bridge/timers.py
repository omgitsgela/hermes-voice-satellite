"""Persistent relative timers for the voice bridge.

Timers are bridge-owned because they need sub-minute relative deadlines. They are
stored atomically and fire through a callback supplied by the announcement plane.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

_LOGGER = logging.getLogger("bridge.timers")
DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "timers.json"
_DURATION_PART = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
}


class TimerLimitError(ValueError):
    """The configured concurrent-timer cap was reached."""


def parse_duration(text: str) -> int:
    """Parse spoken or compact hour/minute/second durations into whole seconds."""
    cleaned = " ".join(str(text or "").lower().strip().split())
    if cleaned in {"half an hour", "half hour"}:
        return 1800
    if not cleaned:
        raise ValueError("duration is required")

    total = 0.0
    consumed: list[tuple[int, int]] = []
    for match in _DURATION_PART.finditer(cleaned):
        value = float(match.group("value"))
        if value <= 0:
            raise ValueError("duration must be greater than zero")
        total += value * _UNIT_SECONDS[match.group("unit").lower()]
        consumed.append(match.span())
    if not consumed:
        raise ValueError(f"could not parse duration {text!r}")

    remainder = list(cleaned)
    for start, end in consumed:
        remainder[start:end] = " " * (end - start)
    leftover = " ".join("".join(remainder).split())
    if leftover not in {"", "and", "for", "for and"}:
        raise ValueError(f"could not parse duration {text!r}")
    seconds = int(round(total))
    if seconds <= 0:
        raise ValueError("duration must be at least one second")
    return seconds


class TimerManager:
    """Durable timer store plus one deadline-driven scheduler task."""

    def __init__(
        self,
        *,
        state_path: Path = DEFAULT_STATE_PATH,
        on_expire: Callable[[dict], object],
        now: Callable[[], float] = time.time,
        max_timers: int = 10,
    ) -> None:
        self.state_path = Path(state_path)
        self.on_expire = on_expire
        self.now = now
        self.max_timers = max_timers
        self._timers = self._load()
        self._changed: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None

    def _load(self) -> list[dict]:
        try:
            payload = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError) as exc:
            _LOGGER.error("could not load timer state from %s: %s", self.state_path, exc)
            return []
        records = payload.get("timers", []) if isinstance(payload, dict) else []
        valid: list[dict] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            try:
                valid.append({
                    "id": str(item["id"]),
                    "label": str(item.get("label") or ""),
                    "created_at": float(item["created_at"]),
                    "due_at": float(item["due_at"]),
                    "duration_seconds": int(item["duration_seconds"]),
                })
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("ignoring malformed timer record: %r", item)
        return sorted(valid, key=lambda item: (item["due_at"], item["id"]))

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        try:
            pending.write_text(json.dumps({"version": 1, "timers": self._timers}, indent=2) + "\n")
            pending.chmod(0o600)
            pending.replace(self.state_path)
        except OSError:
            pending.unlink(missing_ok=True)
            raise

    def _signal_change(self) -> None:
        if self._changed is not None:
            self._changed.set()

    def set_timer(self, seconds: int, label: str = "") -> dict:
        seconds = int(seconds)
        if seconds <= 0:
            raise ValueError("seconds must be greater than zero")
        if len(self._timers) >= self.max_timers:
            raise TimerLimitError(f"at most {self.max_timers} timers may run at once")
        cleaned_label = " ".join(str(label or "").split())[:120]
        created = float(self.now())
        item = {
            "id": f"t{secrets.token_hex(3)}",
            "label": cleaned_label,
            "created_at": created,
            "due_at": created + seconds,
            "duration_seconds": seconds,
        }
        self._timers.append(item)
        self._timers.sort(key=lambda timer: (timer["due_at"], timer["id"]))
        self._save()
        self._signal_change()
        return dict(item)

    def list_timers(self) -> list[dict]:
        now = float(self.now())
        return [
            {**timer, "remaining_seconds": max(0, int(round(timer["due_at"] - now)))}
            for timer in sorted(self._timers, key=lambda item: (item["due_at"], item["id"]))
        ]

    def cancel_timer(
        self,
        *,
        timer_id: Optional[str] = None,
        label: Optional[str] = None,
        all_timers: bool = False,
    ) -> list[dict]:
        selectors = int(bool(timer_id)) + int(bool(label)) + int(bool(all_timers))
        if selectors != 1:
            raise ValueError("provide exactly one of id, label, or all")
        if all_timers:
            cancelled, self._timers = self._timers, []
        elif timer_id:
            cancelled = [timer for timer in self._timers if timer["id"] == timer_id]
            self._timers = [timer for timer in self._timers if timer["id"] != timer_id]
        else:
            needle = " ".join(str(label).lower().split())
            matches = [timer for timer in self._timers if needle in timer["label"].lower()]
            if len(matches) > 1:
                raise ValueError("label matches more than one timer; cancel by id")
            cancelled = matches
            ids = {timer["id"] for timer in matches}
            self._timers = [timer for timer in self._timers if timer["id"] not in ids]
        if cancelled:
            self._save()
            self._signal_change()
        return [dict(timer) for timer in cancelled]

    async def fire_due(self) -> int:
        now = float(self.now())
        due = [timer for timer in self._timers if timer["due_at"] <= now]
        if not due:
            return 0
        due.sort(key=lambda item: (item["due_at"], item["id"]))
        due_ids = {timer["id"] for timer in due}
        self._timers = [timer for timer in self._timers if timer["id"] not in due_ids]
        # Remove first, atomically, so a restart cannot announce the same expiry twice.
        self._save()
        for timer in due:
            lateness = max(0.0, now - timer["due_at"])
            if lateness:
                _LOGGER.info("timer %s expired %.1fs while the bridge was unavailable", timer["id"], lateness)
            result = self.on_expire(dict(timer))
            if inspect.isawaitable(result):
                await result
        return len(due)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._changed = asyncio.Event()
        await self.fire_due()
        self._task = asyncio.create_task(self._run(), name="timer-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._changed = None

    async def _run(self) -> None:
        assert self._changed is not None
        while True:
            await self.fire_due()
            timers = self.list_timers()
            if not timers:
                await self._changed.wait()
                self._changed.clear()
                continue
            delay = max(0.0, timers[0]["due_at"] - float(self.now()))
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=delay)
                self._changed.clear()
            except asyncio.TimeoutError:
                pass


__all__ = ["TimerLimitError", "TimerManager", "parse_duration", "DEFAULT_STATE_PATH"]
