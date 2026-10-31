#!/usr/bin/env python3
"""One physical device must receive an announcement ONCE.

The bug this pins: `_delivery_targets` deduplicated by object identity (`id(sat)`). Two delivery
objects can exist for one device -- the loop's own connection and the registry's lease for that
same device -- and identity-based dedup lets both through. Because `play_wav` holds the playback
lock for the clip's audible duration, the result is not a doubled mix: it is the SAME announcement
played twice, back to back, on one speaker.

Measured on the board before the fix: an 8.48 s announcement occupied the amplifier for ~20 s and
the room heard the sentence twice. That is the "jitter" report.

    .venv/bin/python scripts/test_announce_dedup.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from satellite_gateway.announce import Announcer  # noqa: E402

checks = 0
failures: list[str] = []


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


class Sat:
    """A delivery target. `device_id` is the physical device it speaks through."""

    def __init__(self, device_id, name="sat"):
        self.device_id = device_id
        self.name = name

    def __repr__(self):
        return f"<{self.name} {self.device_id}>"


def announcer(satellites=None, own=None, turn_active=False, turn_owner=None):
    """An Announcer with only the delivery-selection surface wired up.

    Built with __new__ on purpose: this file tests target selection, not the HTTP server or the
    queue, and constructing the full object would need a running loop and a TTS stack.
    """
    a = Announcer.__new__(Announcer)
    a._satellites = satellites
    a._sat = own
    a._turn_is_active = lambda: turn_active
    a._turn_owner = (lambda: turn_owner) if turn_owner else None
    return a


# --- the case that bit: two objects, one device -----------------------------------------------
lease = Sat("esp32-68ee8f4b5988", "lease")
loop_connection = Sat("esp32-68ee8f4b5988", "loop-connection")
targets = announcer(satellites=lambda: [lease, loop_connection])._delivery_targets()
check("two objects for one device collapse to one target", len(targets), 1)
check("the surviving target is the device, not a duplicate", targets[0] is lease, True)

# --- distinct devices must all be announced on ------------------------------------------------
a_dev = Sat("esp32-a", "a")
b_dev = Sat("esp32-b", "b")
targets = announcer(satellites=lambda: [a_dev, b_dev])._delivery_targets()
check("two distinct devices both get it", len(targets), 2)

# --- duplicate objects for a device that appears twice in the lease view ----------------------
same = Sat("esp32-a", "a")
targets = announcer(satellites=lambda: [a_dev, same])._delivery_targets()
check("the same device twice in the view collapses", len(targets), 1)

# --- the conversation's device is excluded while a turn is active -----------------------------
targets = announcer(satellites=lambda: [a_dev, b_dev],
                    turn_active=True, turn_owner="esp32-a")._delivery_targets()
check("the talking device is skipped during a turn", [t.device_id for t in targets], ["esp32-b"])

# --- single-satellite deployments keep the historical shape -----------------------------------
targets = announcer(satellites=None, own=a_dev)._delivery_targets()
check("no fleet view falls back to the loop connection", targets, [a_dev])

# --- a target with no device_id falls back to object identity, and still dedups ---------------
anon1, anon2 = Sat(None, "anon1"), Sat(None, "anon2")
targets = announcer(satellites=lambda: [anon1, anon1])._delivery_targets()
check("anonymous targets dedup by identity", len(targets), 1)
targets = announcer(satellites=lambda: [anon1, anon2])._delivery_targets()
check("distinct anonymous targets both survive", len(targets), 2)

# --- a fleet view that raises must not lose the announcement ---------------------------------
def boom():
    raise RuntimeError("registry unavailable")


targets = announcer(satellites=boom, own=b_dev)._delivery_targets()
check("a broken fleet view falls back to the loop connection", targets, [b_dev])

print(f"{checks} checks")
if failures:
    print("FAILURES:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("OK: announcements reach each physical device exactly once")
