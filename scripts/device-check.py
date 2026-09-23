#!/usr/bin/env python3
"""Report what a voice-satellite audio device actually is, on the machine it is attached to.

Run this ON the satellite host (the Pi), once per candidate device, BEFORE writing any code for it.
The point is to turn "it works on my desk" into a record that can be compared across devices, and
to answer the four questions that decide how much work a device needs:

  1. does it appear as an ALSA sound card, and can it capture 16 kHz mono (what the wake word and
     speech-to-text need);
  2. does it provide playback, since the Pi Zero 2 W has no analogue output of its own;
  3. what LED bus is present (our service drives APA102 over SPI; every ReSpeaker USB array uses
     WS2812, which cannot reuse it);
  4. is there a button (the HAT and the USB arrays expose mute/user buttons that we could use).

Read-only. It opens the capture device briefly with --dump-hw-params, which claims the device for
well under a second and records nothing.

Usage:
    python3 device-check.py                 human summary
    python3 device-check.py --json          machine-readable, for keeping reports side by side
"""

from __future__ import annotations

import glob
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SAT_WAKE_CARD_HINT = "seeed"          # what our current HAT's card is called
SERVICES = ("2mic_leds", "wake-shim", "wyoming-openwakeword", "wyoming-satellite")


def run(cmd: list[str], timeout: int = 10) -> str:
    """Run a command, never raise, return stripped stdout."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "").strip() or (p.stderr or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def cards() -> list[dict]:
    """The ALSA cards, as the kernel reports them."""
    out = []
    text = Path("/proc/asound/cards").read_text() if Path("/proc/asound/cards").exists() else ""
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or not block[0].isdigit():
            continue
        first = block.splitlines()[0]
        idx = first.split()[0]
        name = first.split("[", 1)[1].split("]", 1)[0] if "[" in first else "?"
        out.append({"index": idx, "id": name, "raw": " ".join(first.split())})
    return out


def card_caps(card_id: str) -> dict:
    """Capture/playback formats for a card, by asking ALSA rather than guessing."""
    caps = {"capture": "", "playback": "", "capture_16k_mono": False}
    for direction, tool in (("capture", "arecord"), ("playback", "aplay")):
        binary = shutil.which(tool)
        if not binary:
            continue
        out = run([binary, "-D", f"plughw:CARD={card_id}", "--dump-hw-params", "/dev/null"], timeout=8)
        caps[direction] = out[:600]
        if direction == "capture" and "RATE: [16000]" in out.replace("  ", " "):
            caps["capture_16k_mono"] = True
        elif direction == "capture":
            for line in out.splitlines():
                if line.strip().upper().startswith("RATE") and "16000" in line:
                    caps["capture_16k_mono"] = True
    return caps


def input_devices() -> list[str]:
    """Anything that looks like a button, from the kernel's input list."""
    found = []
    path = Path("/proc/bus/input/devices")
    if path.exists():
        for block in path.read_text().split("\n\n"):
            if not block.strip():
                continue
            name = next((l.split("=", 1)[1] for l in block.splitlines() if l.startswith("N: Name=")), "?")
            handlers = next((l.split("=", 1)[1] for l in block.splitlines() if l.startswith("H: Handlers=")), "")
            if any(k in name.lower() for k in ("button", "gpio", "keys", "hat")):
                found.append(f"{name.strip()} [{handlers.strip()}]")
    return found


def usb_audio() -> list[str]:
    out = run(["lsusb"])
    keep = [l for l in out.splitlines() if any(k in l.lower() for k in ("audio", "mic", "respeaker", "xmos"))]
    return keep or (out.splitlines()[:0])


def main() -> int:
    as_json = "--json" in sys.argv
    card_list = cards()
    suspect = next((c["id"] for c in card_list if SAT_WAKE_CARD_HINT in c["id"]), None)
    suspect = suspect or (card_list[-1]["id"] if card_list else None)

    report = {
        "host": platform.node(),
        "os": run(["sh", "-c", ". /etc/os-release; echo $PRETTY_NAME"]),
        "kernel": platform.release(),
        "arch": f"{platform.machine()} ({platform.architecture()[0]})",
        "python": platform.python_version(),
        "memory_mb": {},
        "cards": card_list,
        "card_under_test": suspect,
        "caps": card_caps(suspect) if suspect else {},
        "spi_devices": sorted(glob.glob("/dev/spidev*")),
        "i2c_devices": sorted(glob.glob("/dev/i2c-*")),
        "input_devices": input_devices(),
        "usb_audio": usb_audio(),
        "thermal": run(["sh", "-c", "vcgencmd measure_temp; vcgencmd get_throttled"]),
        "services": {s: run(["systemctl", "is-active", s]) for s in SERVICES},
        "device_overlay": run(["sh", "-c",
                               "grep -h 'dtoverlay=.*voicecard\\|dtoverlay=respeaker' /boot/firmware/config.txt /boot/config.txt 2>/dev/null"]),
        "what_a_new_device_needs": [
            "capture device name for the satellite's --mic-uri/--wake args (this card's id)",
            "which capture channel to use (processed/beamformed channels differ per device)",
            "playback device name, or an external speaker if it captures only",
            "LED driver: spidev present means our APA102 service can work; none means WS2812/USB LEDs and new code",
        ],
    }
    if Path("/proc/meminfo").exists():
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            if k in ("MemTotal", "MemAvailable"):
                mem[k] = f"{int(v.strip().split()[0])//1024} MB"
        report["memory_mb"] = mem

    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(f"device check on {report['host']}  ({report['os']}, kernel {report['kernel']}, {report['arch']})")
    print(f"  memory: {report['memory_mb']}   thermal: {report['thermal'] or 'n/a (not a Pi)'}")
    print()
    print("  ALSA cards:")
    for c in card_list:
        mark = "  <- under test" if c["id"] == suspect else ""
        print(f"    {c['index']}: {c['raw']}{mark}")
    if suspect:
        caps = report["caps"]
        print()
        print(f"  card '{suspect}' capture: 16 kHz mono available: {caps.get('capture_16k_mono')}")
        for line in (caps.get("capture") or "").splitlines()[:6]:
            print(f"    {line.strip()}")
        print(f"  card '{suspect}' playback: {'present' if caps.get('playback') else 'NOT REPORTED'}")
    print()
    print(f"  LED bus: spidev {report['spi_devices'] or 'none (our APA102 service cannot drive this device)'}")
    print(f"  buttons/input: {report['input_devices'] or 'none detected'}")
    print(f"  USB audio devices: {report['usb_audio'] or 'none detected'}")
    print(f"  device overlay: {report['device_overlay'] or 'none found'}")
    print(f"  services: {report['services']}")
    print()
    print("  What a new device will need plumbed (this is the device-profile list):")
    for item in report["what_a_new_device_needs"]:
        print(f"    - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
