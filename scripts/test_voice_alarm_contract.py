#!/usr/bin/env python3
"""Offline contract check for the voice surface's alarm behavior.

A spoken alarm that is created as a plain cron reminder never reaches the house speaker
and never answers "what alarms do I have". This pins both halves in `VOICE_INSTRUCTIONS`:

* creating one must use the documented mechanism (the alarm script, no agent, local
  delivery), so it speaks through the announcement plane instead of sending a message;
* listing one must be scoped to alarms rather than dumping the whole cron inventory.

The checker is also run against a deliberately broken prompt, so a green result means the
assertions can still fail rather than being satisfied by whatever the prompt happens to say.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.config import VOICE_INSTRUCTIONS  # noqa: E402

# The mechanism clauses. Alarm jobs created without these are reminders: they cost a model
# call, deliver a message, and are never spoken through the house speaker.
REQUIRED = (
    "alarm_fire.py",
    "no_agent",
    "deliver local",
    "alarm:",
    "alarm_list",
)

# A prompt that sends a message instead of using the alarm mechanism is the exact defect.
REMINDER_SHAPED = (
    "reminder and deliver it to telegram",
)


def check_alarm_contract(text: str) -> list[str]:
    """Return the alarm-contract violations in *text*."""
    problems = [f"missing: {needed}" for needed in REQUIRED if needed not in text]
    lowered = text.lower()
    problems += [f"contradicted: {forbidden}" for forbidden in REMINDER_SHAPED if forbidden in lowered]
    return problems


def main() -> int:
    failures = [f"VOICE_INSTRUCTIONS {problem}" for problem in check_alarm_contract(VOICE_INSTRUCTIONS)]

    # Prove the checker bites: a reminder-shaped prompt must be reported as contradicting
    # the contract, not silently accepted. Read it back from a deliberately broken sample.
    broken = "Set an alarm by creating an ordinary reminder and deliver it to telegram."
    if "contradicted: reminder and deliver it to telegram" not in check_alarm_contract(broken):
        print("  FAIL: the checker accepts a reminder-shaped alarm prompt")
        failures.append("checker does not reject a reminder-shaped alarm prompt")

    for failure in failures:
        print("  FAIL:", failure)
    if not failures:
        print("OK: spoken alarms use the alarm mechanism and list only alarms")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
