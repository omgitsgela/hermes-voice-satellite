"""The wake router: wakes from any armed device, one conversation at a time (issue #111).

The fleet leases every registered device and arms every lease, so *any* device can fire its
wake word at any moment. The conversation, however, is one household conversation (decided
2026-10-23): wakes therefore cannot each start their own turn. They queue here, FIFO, and the
loop claims one at a time.

What this module owns:
  * the per-device FIFO of wake records (a wake is never dropped, only deferred);
  * the tentative-claim lifecycle (claim -> resolve -> release losers or retire the winner);
  * the suppression hook: the loop decides, per device, whether a wake is that device's own
    answer echoing back. Suppression is keyed per device; device X's answer must not swallow
    device Y's independent wake.

What it deliberately does NOT own: the audio, the turn machinery, or which device is
`self.sat`. Those stay in bridge/loop.py. The winner keeps its already-open listen stream
(decided 2026-10-23): claiming a record never sends anything to the device. Releases happen
only through the router's resolve step, and only for records the arbitration judged
unfavourable — never for the tentative winner of an unresolved claim.

Milestone A arbitration is arrival order (FIFO). The claim window is what Milestone B's
level-based arbitration will use to gather same-utterance wakes; the plumbing is here so B
lands as a winner-rule change, not a restructure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

_LOGGER = logging.getLogger(__name__)

# How long a tentative claim stays open to later same-utterance detections (Milestone B).
# 0 means "first arrival wins outright" (Milestone A). Config knob: VOICE_WAKE_CLAIM_WINDOW_MS.
DEFAULT_CLAIM_WINDOW_S = 0.0


@dataclass
class WakeRecord:
    """One wake detection, tagged with the device that heard it."""

    device_id: str
    name: str
    arrived_at: float = field(default_factory=time.monotonic)
    detection: object = None      # the wyoming Detection, for context (level) later
    released: bool = False        # arbitration judged this one unfavourable

    # -- arbitration evidence (Milestone B) --------------------------------------
    #
    # Devices carry what they heard in the detection context:
    #   context = {"wake_level": int 0-100, "wake_score": float, "window_ms": int}
    # Levels are device-local fixed scales (the firmware rule), comparable ACROSS devices only
    # for the same utterance inside one claim window — never across time or installations.
    # A missing level (old firmware, a detection relayed without context) reads as None, and
    # None compares equal to None: arbitration degrades to wake score, then arrival order.

    def _context(self) -> dict:
        context = getattr(self.detection, "context", None)
        if isinstance(context, dict):
            return context
        return {}

    @property
    def wake_level(self) -> Optional[int]:
        value = self._context().get("wake_level")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not (0.0 <= float(value) <= 100.0) or float(value) != float(value):  # NaN-proof
            return None
        return int(value)

    @property
    def wake_score(self) -> Optional[float]:
        value = self._context().get("wake_score")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        score = float(value)
        if score != score:  # NaN-proof
            return None
        return score


def pick_winner(records: List["WakeRecord"]) -> Optional["WakeRecord"]:
    """The best-hearing candidate: level first, wake score as tiebreaker, arrival as last resort.

    The rule (issue #32): audio level decides, not wake confidence. A candidate without a
    level never outranks one with a level (a device that measures gets trusted over one that
    does not), and two level-less candidates fall back to exactly the old FIFO order.
    """
    best: Optional[WakeRecord] = None
    best_key = None
    for record in records:
        if record.released:
            continue
        # (level or -1, score or -inf, -arrived) maximised: level dominates, then score,
        # then earlier arrival. -1 sorts every level-less device below any measured one.
        key = (
            -1 if record.wake_level is None else record.wake_level,
            float("-inf") if record.wake_score is None else record.wake_score,
            -record.arrived_at,
        )
        if best_key is None or key > best_key:
            best, best_key = record, key
    return best


SuppressionCheck = Callable[[str], bool]


class WakeRouter:
    """Per-device FIFO of wakes plus the claim/resolve/release lifecycle."""

    def __init__(self, suppress_check: Optional[SuppressionCheck] = None,
                 claim_window_s: float = DEFAULT_CLAIM_WINDOW_S) -> None:
        self._queue: asyncio.Queue[WakeRecord] = asyncio.Queue()
        # Mirror of what is queued, for membership checks without touching queue internals.
        # Records are shared objects, so a release() here is visible in the queue too.
        self._queued: List[WakeRecord] = []
        self._suppress = suppress_check
        self.claim_window_s = claim_window_s
        self._tentative: Optional[WakeRecord] = None
        self._tentative_expires: float = 0.0
        self.submitted_count = 0
        self.suppressed_count = 0

    # -- submission (from per-device supervisors) ---------------------------------

    def submit(self, device_id: str, name: str, detection=None, force: bool = False) -> bool:
        """Queue one wake. Returns False when the loop's per-device suppression rejected it.

        Suppression runs at submission so a self-echo never queues, never becomes a claim, and
        never cancels anyone's stream. The check is the loop's, keyed by device: it knows which
        device is speaking, not which device heard.

        `force` bypasses suppression for a wake the loop already accepted and is deliberately
        re-submitting: the held-wake path (a wake heard while waiting for `played`) chose to
        keep it, so the echo rule does not apply a second time.
        """
        if not force and self._suppress is not None and self._suppress(device_id):
            self.suppressed_count += 1
            return False
        record = WakeRecord(device_id=device_id, name=name, detection=detection)
        self._queue.put_nowait(record)
        self._queued.append(record)
        self.submitted_count += 1
        return True

    # -- claiming (by the conversation loop) ---------------------------------------

    async def claim(self, timeout: Optional[float] = None) -> Optional[WakeRecord]:
        """Pop the next wake and hold it tentatively; None on timeout.

        A tentative claim is deliberately inert: nothing is sent to the device, its stream is
        not touched. It can still be released by resolve() (arbitration judged it unfavourable)
        or retired as the winner. Releases of stale records are skipped so a cancelled loser's
        wake never becomes a turn.
        """
        if self._tentative is not None:
            if self._tentative.released:
                self._tentative = None      # released while nobody was claiming; forget it
            else:
                return self._tentative      # a previous claim is still outstanding
        deadline = time.monotonic() + (timeout if timeout is not None else 3600.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                record = await asyncio.wait_for(self._queue.get(), timeout=min(remaining, 0.5))
            except asyncio.TimeoutError:
                continue
            self._discard(record)
            if record.released:
                _LOGGER.info("wake from %s was cancelled by arbitration; not claiming it",
                             record.device_id)
                continue
            self._tentative = record
            self._tentative_expires = time.monotonic() + self.claim_window_s
            return record

    def has_pending(self, device_id: str) -> bool:
        """True when a not-yet-claimed wake from this device is queued."""
        return any(r.device_id == device_id and not r.released for r in self._queued)

    def _discard(self, record: WakeRecord) -> None:
        """Drop one record from the membership mirror (identity, not equality)."""
        for index, queued in enumerate(self._queued):
            if queued is record:
                self._queued.pop(index)
                return

    # -- arbitration lifecycle (Milestone B plugs in here) ------------------------

    async def resolve_pending(self) -> List[WakeRecord]:
        """Gather same-utterance wakes from *other* devices within the claim window.

        Returns the candidate set including the tentative winner. Milestone A (window 0)
        returns just the winner. Same-device detections never join: one utterance cannot be
        two wakes from one mic.
        """
        winner = self._tentative
        if winner is None:
            return []
        if self.claim_window_s <= 0:
            return [winner]
        candidates: List[WakeRecord] = [winner]
        while True:
            remaining = self._tentative_expires - time.monotonic()
            if remaining <= 0:
                return candidates
            try:
                record = await asyncio.wait_for(self._queue.get(), timeout=min(remaining, 0.5))
            except asyncio.TimeoutError:
                continue
            self._discard(record)
            if record.device_id == winner.device_id:
                # The same mic cannot have heard the same utterance twice: put it back for
                # whoever is next in line rather than joining this claim.
                self._queue.put_nowait(record)
                self._queued.append(record)
                continue
            if not record.released:
                candidates.append(record)

    async def arbitrate(self) -> tuple[Optional[WakeRecord], List[WakeRecord]]:
        """Resolve the tentative claim: gather same-utterance candidates, pick the best listener.

        Returns (winner, losers). Milestone A (window 0) gathers nothing, so the tentative
        claim itself wins and nobody is cancelled. Milestone B gathers the window and applies
        pick_winner — the device that heard the utterance best keeps its stream, and the
        losers' records are already marked released here so no stale wake can claim a turn
        while the loop is cancelling their streams.
        """
        candidates = await self.resolve_pending()
        candidates = [c for c in candidates if not c.released]
        winner = pick_winner(candidates)
        if winner is None:
            return None, []
        losers = [c for c in candidates if c.device_id != winner.device_id]
        for loser in losers:
            loser.released = True
        return winner, losers

    @staticmethod
    def evidence(record: Optional[WakeRecord]) -> str:
        """What a candidate heard, for the decision log: level, score, arrival."""
        if record is None:
            return "(none)"
        level = record.wake_level if record.wake_level is not None else "no-level"
        score = record.wake_score if record.wake_score is not None else "no-score"
        return f"level={level} score={score}"

    def release(self, record: WakeRecord) -> None:
        """Mark one candidate unfavourable. The loop sends the actual cancel to the device."""
        record.released = True

    def retire(self) -> Optional[WakeRecord]:
        """The tentative winner is confirmed as the turn owner: clear it from the router."""
        record, self._tentative = self._tentative, None
        self._tentative_expires = 0.0
        return record
