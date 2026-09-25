"""Device-side ALSA volume control for the voice satellite (issue #10).

This runs on the Pi, where playback actually happens. The bridge renders speech but
the level that governs everything the device emits - our TTS, the connected tone, the
done tone, announcements - is the ALSA level for the output card.

**It enforces its own floor.** The bridge already clamps, and this clamps again,
because the issue's requirement is that no code path anywhere can take the device to
silence. Two independent clamps means a bug or a bad desired value on the host cannot
produce a device that answers nothing. Defence here is cheap; a deaf device is not.

Two values are NOT decided here and must be measured on this device:

- which ALSA control the speaker header actually follows. The codec exposes `PCM`,
  `Line`, `HP`, `HP DAC` and `HPCOM`, and the name is not evidence.
- the audible floor: the bottom of the range has to be a level that can be heard.

`scripts/calibrate-volume.py` establishes both by listening, and writes them to the
calibration file this module reads. Until then, the defaults below are conservative
placeholders, not measurements.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("pi.volume")

MAX_PERCENT = 100

#: Placeholders until measured. See the module docstring.
DEFAULT_FLOOR_PERCENT = 25
DEFAULT_CARD = "seeed2micvoicec"
DEFAULT_CONTROL = "PCM"

#: The calibration the script on this device writes.
CALIBRATION_FILE = Path(
    os.environ.get("VOICE_VOLUME_CALIBRATION")
    or (Path.home() / ".local/state/hermes-voice-satellite" / "volume.json")
)

#: Any "... [90%] ..." in amixer output. The left channel is enough: both channels are
#: set together, so they cannot disagree in normal use.
_PERCENT = re.compile(r"\[(\d{1,3})%\]")

#: Where Debian-family systems keep these tools. `alsactl` lives in **/usr/sbin**, which is
#: in systemd's default service PATH but NOT in a normal login shell's. Resolving against
#: PATH alone therefore means the volume works under the service and the persistence
#: silently fails when the same code is run from a shell - and persistence is exactly the
#: "survives a reboot" requirement, so a quiet failure there is the worst kind.
_STANDARD_BIN_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")


def resolve_binary(binary: str) -> Optional[str]:
    """PATH first, then the standard sbin/bin locations, or None if it is genuinely absent."""
    found = shutil.which(binary)
    if found:
        return found
    for directory in _STANDARD_BIN_DIRS:
        candidate = Path(directory) / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


class VolumeCommandError(RuntimeError):
    """amixer refused or is unavailable. Raised rather than reported as success."""


def parse_percent(output: str) -> Optional[int]:
    """Pull the level out of `amixer sget` output, or None when there is none.

    None rather than 0 on failure, deliberately: "I could not read the level" and "the
    level is zero" must not look the same to a caller deciding whether to speak.
    """
    if not isinstance(output, str):
        return None
    match = _PERCENT.search(output)
    if match is None:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= MAX_PERCENT else None


def load_calibration(path: Optional[Path] = None) -> tuple[int, str, str]:
    """The measured floor, card and control, or the placeholders with a logged warning."""
    target = Path(path) if path is not None else CALIBRATION_FILE
    floor, card, control = DEFAULT_FLOOR_PERCENT, DEFAULT_CARD, DEFAULT_CONTROL
    try:
        raw = json.loads(target.read_text())
    except FileNotFoundError:
        log.info(
            "volume is using the uncalibrated placeholder floor of %d%%; run "
            "scripts/calibrate-volume.py on this device to replace it with a measured value",
            floor,
        )
        return floor, card, control
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s (%s); using the placeholder floor", target, exc)
        return floor, card, control

    if not isinstance(raw, dict):
        log.warning("%s is not a JSON object; using the placeholder floor", target)
        return floor, card, control

    measured = raw.get("floor_percent")
    if isinstance(measured, int) and not isinstance(measured, bool) and 1 <= measured < MAX_PERCENT:
        floor = measured
    elif measured is not None:
        log.warning("calibrated floor_percent %r is out of range; using %d%%", measured, floor)

    for key, fallback in (("card", card), ("control", control)):
        value = raw.get(key)
        if isinstance(value, str) and value.strip() and len(value) <= 64:
            if key == "card":
                card = value.strip()
            else:
                control = value.strip()

    log.info("volume calibrated: floor %d%%, card %s, control %s", floor, card, control)
    return floor, card, control


@dataclass
class AlsaVolume:
    """The one place on this device that may change the output level."""

    card: str = DEFAULT_CARD
    control: str = DEFAULT_CONTROL
    floor: int = DEFAULT_FLOOR_PERCENT
    runner: Callable[..., Any] = field(default=subprocess.run)
    timeout: float = 10.0

    @classmethod
    def from_calibration(cls, **overrides: Any) -> "AlsaVolume":
        floor, card, control = load_calibration()
        kwargs: dict[str, Any] = {"floor": floor, "card": card, "control": control}
        kwargs.update(overrides)
        return cls(**kwargs)

    # -- the clamp ------------------------------------------------------------

    def clamp(self, percent: object) -> int:
        """Raise to the floor, cap at maximum. Cannot return anything silent."""
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            log.warning("refusing a non-numeric volume %r; using the floor", percent)
            return self.floor
        return max(self.floor, min(MAX_PERCENT, int(percent)))

    # -- commands -------------------------------------------------------------

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        resolved = resolve_binary(argv[0])
        if resolved is None:
            raise VolumeCommandError(f"{argv[0]} is not installed on this device")
        argv = [resolved, *argv[1:]]
        try:
            return self.runner(argv, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VolumeCommandError(f"could not run {argv[0]}: {exc}") from exc

    def read(self) -> Optional[int]:
        """The current level, or None if it cannot be read."""
        try:
            result = self._run(["amixer", "-c", self.card, "sget", self.control])
        except VolumeCommandError as exc:
            log.warning("%s", exc)
            return None
        if result.returncode != 0:
            log.warning(
                "could not read %s on card %s: %s",
                self.control, self.card, (result.stderr or "").strip()[:120],
            )
            return None
        return parse_percent(result.stdout or "")

    def set(self, percent: object) -> int:
        """Apply a level, clamped. Returns what was applied, never what was asked for.

        Raises on failure: reporting a level the device did not take would make the
        spoken confirmation a lie, and this feature's whole value is that the device
        tells you the truth about whether it can be heard.
        """
        level = self.clamp(percent)
        result = self._run(["amixer", "-c", self.card, "sset", self.control, f"{level}%"])
        if result.returncode != 0:
            raise VolumeCommandError(
                f"amixer could not set {self.control} on card {self.card} to {level}%: "
                f"{(result.stderr or '').strip()[:160]}"
            )
        return level

    def store(self) -> bool:
        """Persist the level with `alsactl store`, so it survives a reboot.

        An ALSA level resets on reboot unless it is written. Returns False rather than
        raising: a failed store means the level works now but will not persist, which is
        worth a warning, not a failed volume change.
        """
        try:
            result = self._run(["alsactl", "store"])
        except VolumeCommandError as exc:
            log.warning("%s", exc)
            return False
        if result.returncode != 0:
            log.warning(
                "alsactl store failed, so the level will not survive a reboot: %s",
                (result.stderr or "").strip()[:160],
            )
            return False
        return True

    def apply(self, percent: object) -> int:
        """Set and persist. Returns the applied level."""
        level = self.set(percent)
        self.store()
        return level


__all__ = [
    "AlsaVolume", "CALIBRATION_FILE", "DEFAULT_CARD", "DEFAULT_CONTROL",
    "DEFAULT_FLOOR_PERCENT", "MAX_PERCENT", "VolumeCommandError",
    "load_calibration", "parse_percent",
]
