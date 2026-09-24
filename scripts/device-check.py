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
import re
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
    """The ALSA cards, as the kernel reports them.

    Parsed line by line, not by blank-line blocks: /proc/asound/cards has no blank line between
    entries, so block splitting collapses every card into one and reports only the first. That bug
    made the first baseline report claim the device under test was HDMI audio. Card ids are stripped
    of the kernel's column padding, because the unpadded name is what belongs in
    `plughw:CARD=<id>`.
    """
    out = []
    path = Path("/proc/asound/cards")
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or not stripped[0].isdigit() or "[" not in stripped:
            continue
        idx = stripped.split()[0]
        name = stripped.split("[", 1)[1].split("]", 1)[0].strip()
        out.append({"index": idx, "id": name, "raw": stripped})
    return out


def parse_hw_params(out: str) -> dict:
    """Pull the RATE and CHANNELS ranges out of an arecord/aplay --dump-hw-params dump.

    The dump prints hardware ranges, for example "RATE: [16000 48000]" or "CHANNELS: [2 2]". A card
    that advertises only two channels cannot be captured mono on that node, which matters here
    because our satellite captures mono and a device that is stereo-only needs its capture channel
    chosen explicitly (see the device-profile list at the end of this report). Reading the range is
    the difference between "this card works" and "this card works if you select a channel".
    """
    found = {"rates": None, "channels": None, "raw_lines": []}
    for line in out.splitlines():
        stripped = line.strip()
        head = stripped.upper().split(":")[0].strip() if ":" in stripped else ""
        if head not in ("RATE", "CHANNELS"):
            continue
        found["raw_lines"].append(stripped)
        nums = [int(n) for n in re.findall(r"\d+", stripped.split(":", 1)[1] if ":" in stripped else "")]
        if not nums:
            continue
        key = "rates" if head == "RATE" else "channels"
        found[key] = (min(nums), max(nums)) if len(nums) > 1 else (nums[0], nums[0])
    return found


def measure_capture(card_id: str, rate: int, channels: int, seconds: int = 1) -> str:
    """Actually capture a moment of audio and report what happened.

    Reading hardware parameters tells you what the node advertises; only opening it tells you what
    it does. This is what a new device should be judged on, and the checklist says to run this check
    before starting the services for exactly that reason. Returns "" on success, or the reason it
    failed. A busy card is reported as busy, never as unsupported.
    """
    binary = shutil.which("arecord")
    if not binary:
        return "arecord not installed"
    out = run([binary, "-D", "plughw:CARD=" + card_id, "-r", str(rate), "-c", str(channels),
               "-f", "S16_LE", "-d", str(seconds), "/dev/null"], timeout=15)
    low = out.lower()
    if "busy" in low:
        return "in use by another process"
    if "unknown" in low or "no such" in low or "unable" in low or "error" in low:
        return out.splitlines()[0].strip()[:120] if out else "failed"
    return ""


def card_caps(card_id: str) -> dict:
    """Capture/playback formats for a card, by asking ALSA rather than guessing.

    A card in use cannot be opened: on the current device the running satellite holds the
    microphone, so `arecord` returns "Device or resource busy" and a naive checker would report that
    the card cannot capture at all. That is a false negative, and it would appear on exactly the
    devices we care about, so the busy case is named explicitly and we report what the card is in
    use AS, read from the satellite unit, which is the real answer for "what is this device doing".
    """
    caps = {"capture": "", "playback": "", "capture_16k_mono": False, "busy": False, "in_use_as": "",
            "hw": {"rates": None, "channels": None, "raw_lines": []},
            "capture_mono": None, "capture_stereo": None,
            "measured_16k_mono": None, "measured_16k_stereo": None}
    for direction, tool in (("capture", "arecord"), ("playback", "aplay")):
        binary = shutil.which(tool)
        if not binary:
            continue
        out = run([binary, "-D", f"plughw:CARD={card_id}", "--dump-hw-params", "/dev/null"], timeout=8)
        if "busy" in out.lower():
            caps["busy"] = True
            caps[direction] = (
                f"{out}\n  NOTE: this card is in use, so its parameters cannot be measured right now."
                " That is expected while the voice stack is running: either stop the satellite"
                " services first, or run this check on a device before starting them."
            )
            continue
        caps[direction] = out[:600]
        if direction == "capture":
            hw = parse_hw_params(out)
            caps["hw"] = hw
            lo, hi = hw["channels"] or (None, None)
            # A hardware range is not the same as an achievable format, so record both, and say
            # plainly when mono is impossible on this node rather than leaving it implied. (issue #34)
            caps["capture_mono"] = (lo == 1) if lo is not None else None
            caps["capture_stereo"] = (hi >= 2) if hi is not None else None
            rate_lo, rate_hi = hw["rates"] or (None, None)
            caps["capture_16k_mono"] = bool(
                rate_lo is not None and rate_lo <= 16000 <= rate_hi
                and (caps["capture_mono"] is not False)
            )
            # Measured, not advertised: only meaningful when nothing else holds the card.
            caps["measured_16k_mono"] = measure_capture(card_id, 16000, 1) or "ok"
            caps["measured_16k_stereo"] = measure_capture(card_id, 16000, 2) or "ok"

    # What the running stack actually opens, which is the useful fact when the card is busy.
    unit = run(["sh", "-c", "systemctl cat wyoming-satellite.service 2>/dev/null | grep ExecStart"])
    for token in unit.split():
        if "CARD=" in token:
            caps["in_use_as"] = token.strip("'\"")
            if caps["busy"]:
                caps["capture"] += f"\n  in use as: {caps['in_use_as']} (from wyoming-satellite.service)"
            break
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
        hw = caps.get("hw") or {}
        rates, channels = hw.get("rates"), hw.get("channels")
        print(f"  card '{suspect}' hardware ranges: rates {rates or 'not reported'}, "
              f"channels {channels or 'not reported'}")
        print(f"  card '{suspect}' capture: 16 kHz mono available: {caps.get('capture_16k_mono')}"
              f"   (mono {'yes' if caps.get('capture_mono') else 'NO' if caps.get('capture_mono') is False else '?'}"
              f", stereo {'yes' if caps.get('capture_stereo') else 'no'})")
        m_mono, m_stereo = caps.get("measured_16k_mono"), caps.get("measured_16k_stereo")
        if m_mono or m_stereo:
            print(f"  card '{suspect}' measured capture: 16k mono {m_mono or 'ok'}, 16k stereo {m_stereo or 'ok'}")
        for line in (caps.get("capture") or "").splitlines()[:6]:
            print(f"    {line.strip()}")
        if caps.get("capture_mono") is False:
            print("    NOTE: this node cannot capture mono. Choose a capture channel explicitly"
                  " (see the device-profile list below), or the satellite will not get audio.")
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
