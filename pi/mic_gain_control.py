"""Device-side ALSA microphone input gain for the voice satellite (issue #6).

Sibling of `volume_control.py`, which owns the OUTPUT level. This owns the INPUT level:
the amplifier in front of the microphone.

It is deliberately the same shape as AlsaVolume and shares the same honesty rule: it never
reports a level it did not set, and it never invents a control. The one thing it cannot know
at import time is *which* amixer control the mic input follows on a given card. On some codecs
it is `Mic`, on others `Capture`, on others a named per-channel control - and the ReSpeaker
voice card here may have no usable input control at all (capture is taken by an `arecord`
command line instead). So the control name is resolved from the calibration/environment, and
`read()`/`set()` return `None`/raise when the control is absent, which the adapter turns into
an honest `degraded` report rather than a fake `healthy`.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("pi.mic_gain")

MAX_PERCENT = 100

#: Placeholder until the input control on this card is identified. Empty means "no known
#: input control on this card", which is a legitimate state: capture may be an arecord
#: command line with no amixer input control to drive.
DEFAULT_CARD = "seeed2micvoicec"
DEFAULT_CONTROL = ""

#: Where the input-control name is recorded, if the maintainer has identified one. Read at
#: runtime; empty string means absent.
ENV_CONTROL = "VOICE_MIC_GAIN_CONTROL"

_STANDARD_BIN_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")
_PERCENT = re.compile(r"\[(\d{1,3})%\]")


def resolve_binary(binary: str) -> Optional[str]:
    found = shutil.which(binary)
    if found:
        return found
    for directory in _STANDARD_BIN_DIRS:
        candidate = os.path.join(directory, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def parse_percent(output: str) -> Optional[int]:
    if not isinstance(output, str):
        return None
    match = _PERCENT.search(output)
    if match is None:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= MAX_PERCENT else None


class VolumeGainError(RuntimeError):
    """The input control is absent or amixer refused. Raised, never reported as success."""


@dataclass
class AlsaMicGain:
    """The one place on this device that may change the microphone input level."""

    card: str = DEFAULT_CARD
    control: str = ""
    runner: Callable[..., Any] = field(default=subprocess.run)
    timeout: float = 10.0

    @classmethod
    def from_env(cls, **overrides: Any) -> "AlsaMicGain":
        control = os.environ.get(ENV_CONTROL, "").strip()
        kwargs: dict[str, Any] = {"control": control}
        kwargs.update(overrides)
        return cls(**kwargs)

    def has_control(self) -> bool:
        """Whether this card exposes a drivable input control at all."""
        return bool(self.control)

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        resolved = resolve_binary(argv[0])
        if resolved is None:
            raise VolumeGainError(f"{argv[0]} is not installed on this device")
        argv = [resolved, *argv[1:]]
        try:
            return self.runner(argv, capture_output=True, text=True,
                               timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VolumeGainError(f"could not run {argv[0]}: {exc}") from exc

    def read(self) -> Optional[int]:
        """The current input level, or None when the control is absent or unreadable."""
        if not self.has_control():
            return None
        try:
            result = self._run(["amixer", "-c", self.card, "sget", self.control])
        except VolumeGainError as exc:
            log.warning("%s", exc)
            return None
        if result.returncode != 0:
            log.warning(
                "could not read %s on card %s (control may not exist): %s",
                self.control, self.card, (result.stderr or "").strip()[:120],
            )
            return None
        return parse_percent(result.stdout or "")

    def set(self, percent: object) -> int:
        """Apply a level and read the hardware back. Returns what the control confirmed.

        Returns the level the control actually took, not the one requested: ALSA controls
        can quantize percentages, so a successful `amixer sset` is not proof the requested
        percentage was applied. Raises VolumeGainError when the control is absent, amixer
        refuses, or the result cannot be read back: reporting a gain the device did not take
        would make the convergence state a lie.
        """
        if not self.has_control():
            raise VolumeGainError("no microphone input control configured on this device")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            raise VolumeGainError(f"mic gain must be a number, got {percent!r}")
        level = max(0, min(MAX_PERCENT, int(percent)))
        result = self._run(["amixer", "-c", self.card, "sset", self.control, f"{level}%"])
        if result.returncode != 0:
            raise VolumeGainError(
                f"amixer could not set {self.control} on card {self.card} to {level}%: "
                f"{(result.stderr or '').strip()[:160]}"
            )
        confirmed = self.read()
        if confirmed is None:
            raise VolumeGainError(
                f"amixer set {self.control} but the level could not be read back; "
                "not reporting a value the hardware did not confirm")
        if confirmed != level:
            log.warning(
                "mic gain: asked for %d%% but the control confirmed %d%% (the hardware "
                "quantizes; reporting the confirmed value)", level, confirmed)
        return confirmed


__all__ = [
    "AlsaMicGain", "VolumeGainError", "parse_percent", "resolve_binary",
    "MAX_PERCENT", "ENV_CONTROL",
]
