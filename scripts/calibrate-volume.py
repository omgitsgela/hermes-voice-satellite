#!/usr/bin/env python3
"""Find the satellite's real volume control and its audible floor (issue #10).

Run this **on the satellite**, not on the bridge. It is deliberately standalone: stdlib
only, no imports from this repository, so it can be copied to the device on its own
without a dependency chain. That is why it duplicates a little amixer parsing instead of
importing `pi/volume_control.py` - a diagnostic that cannot run is worth nothing.

Two things it establishes, because neither can be reasoned about from a desk:

1. **Which ALSA control the speaker follows.** The codec exposes `PCM`, `Line`, `HP`,
   `HP DAC` and `HPCOM`. The name is not evidence; only listening is.
2. **The audible floor.** The lowest level you can actually hear. A percentage that is
   effectively silent is not a floor, it is a device that has stopped answering.

Usage, on the device:

    python3 calibrate-volume.py --enumerate          # what controls exist
    python3 calibrate-volume.py --probe              # step each one, listen, choose
    python3 calibrate-volume.py --floor --control PCM  # sweep down, find where it vanishes
    python3 calibrate-volume.py --show               # what is recorded now

Each phase writes nothing until you confirm. The result goes to
`~/.local/state/hermes-voice-satellite/volume.json`, which is what the device-side
volume code reads.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

DEFAULT_CARD = "seeed2micvoicec"
MAX_PERCENT = 100
STATE_DIR = Path(os.environ.get("VOICE_STATE_DIR")
                 or (Path.home() / ".local/state/hermes-voice-satellite"))
CALIBRATION_FILE = STATE_DIR / "volume.json"

#: Tones that already exist on a satellite built from this project. Any one will do;
#: the point is a steady, recognisable sound. A missing file is not fatal, we just
#: fall back to a generated sine.
CANDIDATE_TONES = [
    Path("/home/wyoming1/sounds/awake.wav"),
    Path("/home/wyoming1/wyoming-satellite/sounds/awake.wav"),
]

#: The controls the issue names. Probed in this order, because the earlier ones are the
#: more likely playback path, but all of them are tested.
CANDIDATE_CONTROLS = ["PCM", "Line", "HP", "HP DAC", "HPCOM"]

_PERCENT = re.compile(r"\[(\d{1,3})%\]")
_CONTROL_LINE = re.compile(r"Simple mixer control '(?P<name>[^']+)'")


def run(argv: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _need(binary: str) -> None:
    if shutil.which(binary) is None:
        raise SystemExit(f"{binary} is not installed on this device; cannot continue")


def parse_percent(output: str) -> Optional[int]:
    match = _PERCENT.search(output or "")
    return int(match.group(1)) if match else None


def list_controls(card: str) -> list[str]:
    """Simple playback controls on the card, in amixer's own order."""
    result = run(["amixer", "-c", card, "scontrols"])
    if result.returncode != 0:
        return []
    return _CONTROL_LINE.findall(result.stdout or "")


def read_level(card: str, control: str) -> Optional[int]:
    result = run(["amixer", "-c", card, "sget", control])
    if result.returncode != 0:
        return None
    return parse_percent(result.stdout or "")


def set_level(card: str, control: str, percent: int) -> bool:
    result = run(["amixer", "-c", card, "sset", control, f"{percent}%"])
    return result.returncode == 0


def play_tone(seconds: float = 1.5) -> bool:
    """Play something recognisable through the satellite's own card and control."""
    _need("aplay")
    for tone in CANDIDATE_TONES:
        if tone.is_file():
            result = run(["aplay", "-q", "-D", f"plughw:CARD={DEFAULT_CARD},DEV=0", str(tone)],
                         timeout=seconds + 10)
            return result.returncode == 0
    # Fall back to a generated tone so the script still works on a bare device.
    if shutil.which("speaker-test") is not None:
        result = run(["speaker-test", "-D", f"plughw:CARD={DEFAULT_CARD},DEV=0",
                      "-t", "sine", "-f", "440", "-l", "1"], timeout=seconds + 10)
        return result.returncode == 0
    print("  (no test tone available: install aplay or speaker-test, or place a wav at "
          f"{CANDIDATE_TONES[0]})")
    return False


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit("stopped; nothing was written")


def saved() -> dict:
    try:
        raw = json.loads(CALIBRATION_FILE.read_text())
        return raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def write_calibration(floor: int, control: str, card: str, *, alarm_min: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "floor_percent": int(floor),
        "alarm_min_percent": int(alarm_min),
        "control": control,
        "card": card,
        "measured_by": "calibrate-volume.py",
        "note": (
            "floor_percent and control were established by listening on this device. "
            "Do not edit them to values nobody has heard."
        ),
    }
    tmp = CALIBRATION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CALIBRATION_FILE)
    os.chmod(CALIBRATION_FILE, 0o600)


# ---------------------------------------------------------------- phases

def phase_enumerate(card: str) -> int:
    _need("amixer")
    print(f"Controls on card {card}:")
    controls = list_controls(card)
    if not controls:
        print("  none found. Is the card name right? Try: aplay -l")
        return 1
    for name in controls:
        level = read_level(card, name)
        marker = "  <-- likely playback path" if name in CANDIDATE_CONTROLS else ""
        shown = "unreadable" if level is None else f"{level}%"
        print(f"  {name:<12} {shown}{marker}")
    print()
    print("The name is not evidence. Use --probe to find which one the speaker follows.")
    return 0


def phase_probe(card: str) -> int:
    """Step each candidate down and up while the operator listens.

    Deliberately does not try to be clever with a microphone: the satellite's own mic
    would hear its own speaker, but room noise, echo and the mic's own gain make that
    far less reliable than asking the person standing there.
    """
    _need("amixer")
    controls = [c for c in list_controls(card) if c in CANDIDATE_CONTROLS] or list_controls(card)
    if not controls:
        print(f"No controls on card {card}.")
        return 1

    print("For each control, a tone plays at a HIGH level then a LOW level.")
    print("Say which control made the speaker change.\n")
    original: dict[str, Optional[int]] = {}
    for name in controls:
        original[name] = read_level(card, name)
        start = original[name] if original[name] and original[name] >= 40 else 60
        print(f"--- {name} " + "-" * (50 - len(name)))
        if not set_level(card, name, 90):
            print("  could not set this control; skipping\n")
            continue
        print("  loud...")
        play_tone()
        set_level(card, name, 15)
        print("  quiet...")
        play_tone()
        if original[name] is not None:
            set_level(card, name, original[name])
        answer = ask(f"  did the volume change when setting {name}? [y/N/quit] ").lower()
        if answer in ("quit", "q"):
            print("stopped; nothing was written")
            return 1
        if answer in ("y", "yes"):
            print(f"\n==> The speaker follows '{name}'.\n")
            print(f"Now find the floor:  python3 {Path(__file__).name} --floor --control '{name}'")
            return 0
    print("\nNone of the controls changed the speaker.")
    print("Try: aplay -l    (confirm the card name), then re-run --enumerate.")
    return 1


def phase_floor(card: str, control: str) -> int:
    """Sweep down from a comfortable level and find where the tone disappears."""
    _need("amixer")
    loudest = read_level(card, control)
    print(f"Finding the audible floor for '{control}' on card {card}.")
    print("A tone plays at each level. Say when you can no longer hear it.\n")

    candidate = 60
    if not set_level(card, control, candidate):
        print(f"Could not set '{control}'. Run --enumerate to check the name.")
        return 1

    step = 15
    last_audible = candidate
    while step >= 1:
        level = max(1, candidate)
        set_level(card, control, level)
        print(f"  level {level}%")
        play_tone(1.0)
        answer = ask(f"  could you hear that? [Y/n/quit] ").lower()
        if answer in ("quit", "q"):
            if loudest is not None:
                set_level(card, control, loudest)
            print("stopped; nothing was written")
            return 1
        heard = answer in ("", "y", "yes")
        if heard:
            last_audible = level
            candidate = level + step
            if candidate >= MAX_PERCENT:
                print("\nStill audible at the top of the range; no floor found below it.")
                if loudest is not None:
                    set_level(card, control, loudest)
                return 1
        else:
            candidate = level - step
            if candidate < 1:
                candidate = 1
        step = 15 if step == 15 else max(1, step // 2)
        if step == 15 and not heard:
            step = 7

    floor = max(1, last_audible)
    print(f"\nLowest level you could hear: {floor}%")
    print("The floor is stored as that level, so every volume command lands at or above it.")

    default_alarm = max(floor, 60)
    alarm_answer = ask(f"Minimum alarm level (alarms override upward, default {default_alarm}%)? ").strip()
    alarm_min = default_alarm
    if alarm_answer.isdigit():
        alarm_min = max(floor, min(MAX_PERCENT, int(alarm_answer)))

    confirm = ask(f"Write floor={floor}%, alarm minimum={alarm_min}%, control='{control}'? [y/N] ").lower()
    if confirm not in ("y", "yes"):
        if loudest is not None:
            set_level(card, control, loudest)
        print("nothing was written")
        return 1

    write_calibration(floor, control, card, alarm_min=alarm_min)
    if loudest is not None:
        set_level(card, control, loudest)
    print(f"\nWrote {CALIBRATION_FILE}")
    print("Verify it survives a reboot before calling this done:")
    print("  sudo systemctl restart wyoming-satellite   # or reboot")
    print(f"  python3 {Path(__file__).name} --show")
    return 0


def phase_show() -> int:
    data = saved()
    if not data:
        print(f"Nothing recorded at {CALIBRATION_FILE}.")
        print("The device is using placeholder values. Run --probe then --floor.")
        return 1
    print(f"Calibration at {CALIBRATION_FILE}:")
    for key in ("floor_percent", "alarm_min_percent", "card", "control", "measured_by"):
        if key in data:
            print(f"  {key:<18} {data[key]}")
    floor, control = data.get("floor_percent"), data.get("control")
    if isinstance(floor, int) and isinstance(control, str) and data.get("card"):
        card = data["card"]
        current = read_level(card, control)
        print()
        print(f"  current level      {'unreadable' if current is None else str(current) + '%'}")
        if isinstance(current, int) and current < floor:
            print(f"  WARNING: the current level is below the stored floor of {floor}%. "
                  "Set it up, or the device may be too quiet to use.")
    print()
    print("Reboot check: the level must survive a real reboot, not just a service restart.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find the satellite's real volume control and its audible floor.")
    parser.add_argument("--card", default=DEFAULT_CARD, help=f"ALSA card (default {DEFAULT_CARD})")
    parser.add_argument("--control", default=None, help="the control to calibrate for --floor")
    parser.add_argument("--enumerate", action="store_true", help="list the card's controls")
    parser.add_argument("--probe", action="store_true", help="find which control drives the speaker")
    parser.add_argument("--floor", action="store_true", help="find the audible floor")
    parser.add_argument("--show", action="store_true", help="show the recorded calibration")
    args = parser.parse_args()

    if args.show:
        return phase_show()
    if args.enumerate:
        return phase_enumerate(args.card)
    if args.probe:
        return phase_probe(args.card)
    if args.floor:
        control = args.control or saved().get("control")
        if not isinstance(control, str) or not control:
            print("--floor needs --control (run --probe first, or --enumerate to see the names).")
            return 1
        return phase_floor(args.card, control)

    parser.print_help()
    print("\nStart with:  --enumerate, then --probe, then --floor")
    return 1


if __name__ == "__main__":
    sys.exit(main())
