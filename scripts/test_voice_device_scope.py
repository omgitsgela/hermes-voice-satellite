#!/usr/bin/env python3
"""Offline contract check for the voice surface's device scope and guessing rules.

This pins the rules a live failure needed. A spoken "volume five" was mis-heard as a fragment;
the Gateway's deliberately conservative volume matcher declined it, correctly; the model then
invented the temperature 65, asked the person to confirm it, read their repeat of the *same
volume request* as agreement, and set the thermostat. The volume never changed, and a device the
person never named did.

The clauses below are what make that chain impossible to follow:

* a volume request is this device's own playback level, never another device in the house;
* a device the person did not name is never changed;
* a clarifying question may not introduce a value the person never said;
* a repeat is the same request, not agreement to the assistant's own guess;
* a garbled or fragmentary utterance does not become a state change.

The checker is also run against a deliberately under-specified prompt, so a green result means
the assertions can still fail rather than being satisfied by whatever the prompt happens to say.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from satellite_gateway.config import VOICE_INSTRUCTIONS  # noqa: E402

REQUIRED = (
    "volume request is about this device's own playback level",
    "never change a device the person did not name",
    "never invent a number and offer it back as a question",
    "never agreement to a guess",
    "ask one short question rather than acting",
)

# Wording that only appeared in the failure: a prompt carrying this alone would still let a
# bare fragment become a thermostat write. Used to prove the checker bites.
UNSCOPED_PROMPT = (
    "If a request is ambiguous, pick the most likely meaning and answer it. "
    "For a smart home action, reply with one short line naming what you did."
)


def check_device_scope_contract(text: str) -> list[str]:
    """Return the device-scope contract violations in *text*.

    Matching is case-insensitive: the rule is the rule regardless of whether a clause starts a
    sentence, and pinning capitalization would make the check brittle without making it stricter.
    """
    lowered = text.lower()
    return [f"missing: {needed}" for needed in REQUIRED if needed.lower() not in lowered]


def main() -> int:
    failures = list(check_device_scope_contract(VOICE_INSTRUCTIONS))

    # Prove the checker bites: the wording that was live during the failure has none of the
    # device-scope clauses, so it must be reported as violating every one of them.
    expected = len(REQUIRED)
    observed = len(check_device_scope_contract(UNSCOPED_PROMPT))
    if observed != expected:
        print(f"  FAIL: the checker accepts an unscoped prompt ({observed} of {expected} reported)")
        failures.append("checker does not reject an unscoped prompt")

    if not failures:
        print("OK: device scope, no-invented-values and repeat rules are pinned")
        return 0
    for failure in failures:
        print("  FAIL:", failure)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
