"""Device-side hardware adapter for the configuration plane (issue #10 and #6).

The configuration agent deliberately has no idea how to change hardware: it validates,
persists and reports. This module is the part that knows how, injected as the agent's
`apply` callback.

**It reports partial application honestly.** Only `playback_volume` has a device-side
path today. The other settings are accepted by the schema and staged, but until each one
has an implementation here the adapter says `degraded` and names them, rather than
returning a healthy status that would make the plane's `converged` flag a lie. That is
the whole reason the plane distinguishes desired from reported.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

try:  # package import in the repo's tests; sibling import when installed beside the runner
    from .volume_control import AlsaVolume, VolumeCommandError
except ImportError:  # pragma: no cover - this is the path the device actually takes
    from volume_control import AlsaVolume, VolumeCommandError

log = logging.getLogger("pi.device_apply")

#: Settings this adapter can actually apply to hardware, right now.
APPLIABLE = ("playback_volume",)

#: Settings the schema accepts but that have no device-side path yet. Named so the
#: report can say which ones, instead of a bare "something did not apply".
NOT_YET_APPLIABLE = ("wake_word", "wake_threshold", "mic_gain", "led_brightness")


def make_apply_adapter(volume: Optional[AlsaVolume] = None):
    """Build the apply callback the configuration agent calls.

    The returned callable takes the validated settings and returns
    `(actual, health, error)`, matching what DeviceConfigAgent expects.
    """
    alsa = volume or AlsaVolume.from_calibration()

    def apply(settings: dict[str, Any]) -> tuple[dict, str, Optional[str]]:
        applied: dict[str, Any] = {}
        problems: list[str] = []

        if "playback_volume" in settings:
            # The setting arrives on the USER scale (1-100), where 1 is the quietest this
            # hardware can be. AlsaVolume works in RAW, which is what the ALSA control
            # takes, so the conversion happens here and nowhere else on this side.
            requested = settings["playback_volume"]
            raw = alsa.user_to_raw(requested)
            try:
                applied_raw = alsa.apply(raw)
            except VolumeCommandError as exc:
                # Do not report a level we did not set. The caller turns this into an
                # error report, and the plane will show the device as unconverged.
                problems.append(f"playback_volume: {exc}")
            else:
                # Report back on the user scale, so the number means the same thing on the
                # bridge as it does on the device and convergence compares like with like.
                applied_user = alsa.raw_to_user(applied_raw)
                applied["playback_volume"] = applied_user
                if applied_user != requested:
                    # Worth surfacing: the floor raised the requested level, and a
                    # silently corrected setting is how someone concludes the setting
                    # "does not work".
                    problems.append(
                        f"playback_volume was raised from {requested} to {applied_user} by the "
                        "audible floor; the device must stay audible"
                    )
                    log.warning(
                        "playback_volume %s was raised to %s by the floor (raw %s)",
                        requested, applied_user, applied_raw,
                    )

        for name in settings:
            if name in APPLIABLE:
                continue
            if name in NOT_YET_APPLIABLE:
                problems.append(f"{name}: no device-side adapter yet, staged only")

        if any(name in settings for name in NOT_YET_APPLIABLE):
            health = "degraded"
        elif problems:
            # Only floor adjustments or nothing: the applied values are real, so this is
            # healthy, and the notes explain the adjustment.
            health = "healthy" if applied else "error"
        else:
            health = "healthy"

        if not applied and not problems:
            # Nothing asked for that this adapter handles. Not an error.
            return {}, "healthy", None

        error = "; ".join(problems) if problems else None
        return applied, health, error

    return apply


__all__ = ["APPLIABLE", "NOT_YET_APPLIABLE", "make_apply_adapter"]
