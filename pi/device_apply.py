"""Device-side hardware adapter for the configuration plane (issues #10 and #6).

The configuration agent deliberately has no idea how to change hardware: it validates,
persists and reports. This module is the part that knows how, injected as the agent's
`apply` callback. It turns a validated set of desired settings into real device changes and
reports, honestly, what it actually did.

**The honesty rule is the whole point.** A device that cannot apply a setting must not claim
it did. Every setting that is applied for real is reported with the value the hardware
actually took; anything that could not be applied is named in the report with a `degraded`
or `error` health, never a `healthy` that would make the plane's `converged` flag a lie. A
restart-class value only counts as *applied* once the service it belongs to has actually been
restarted with it: a value written to the env file but whose restart failed stays out of
`applied`, so it remains retryable rather than being reported as converged.

**Two tiers of change, by whether they bounce a device service:**

- **Immediate** (no restart): `playback_volume` (ALSA output, via `volume_control`) and
  `mic_gain` (ALSA input, via `mic_gain_control`). Written straight to the hardware and read
  back; no service is touched.
- **Restart-class** (durable, one coupled restart): `led_brightness`, `wake_threshold` and
  `wake_word` are written to the device's env file and applied by restarting the coupled audio
  group. Durability is deliberate: a value written to the env file survives a reboot, so a
  setting the operator chose does not silently revert on the next power cycle.

**Idempotency against the full profile.** The bridge transmits the complete merged desired
state on every delivery, not just the change that triggered it. So this adapter must not
treat "the setting is present in the profile" as "the setting changed." It compares each
setting against the state that is currently in force (the hardware level for the immediate
tier; the env file, plus what this process has already brought it to, for the restart-class
tier) and acts only when the desired value actually differs. A volume-only fleet write
therefore never touches the LED or wake services. Without this, every single delivery - even
one that changed only the volume - would bounce the whole fleet's audio stack, which is
precisely what the bridge's pacing of restart-class writes is meant to prevent.

**Why the coupled group, and why this process survives it.** The audio services form a
dependency cycle and must be restarted together, one group, or the restart leaves a unit dead
(a real, past failure). `outbound-satellite` - the process running this code - is *not* in that
group: it dials the bridge and its relay re-arms through a satellite restart, so it can restart
the audio group from the outside and still hold the configuration connection that carries this
very report back. That separation is what makes a device-side restart adapter safe at all.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

try:  # package import in the repo's tests; sibling import when installed beside the runner
    from .volume_control import AlsaVolume, VolumeCommandError
    from .mic_gain_control import AlsaMicGain, VolumeGainError
except ImportError:  # pragma: no cover - this is the path the device actually takes
    from volume_control import AlsaVolume, VolumeCommandError
    from mic_gain_control import AlsaMicGain, VolumeGainError

log = logging.getLogger("pi.device_apply")

#: The coupled audio stack, restarted as ONE group for a restart-class setting. Deliberately
#: excludes `outbound-satellite`: that is the process applying this change, and it must stay up
#: to report the result. Restarting it too would drop the very connection carrying the report.
COUPLED_AUDIO_UNITS = ("wyoming-satellite", "wyoming-openwakeword", "wake-shim", "2mic_leds")

#: Which units a given setting's restart must cover. The LED service is a subset of the coupled
#: group; a wake change needs the whole group in dependency order.
_UNITS_FOR = {
    "led_brightness": ("2mic_leds",),
    "wake_threshold": COUPLED_AUDIO_UNITS,
    "wake_word": COUPLED_AUDIO_UNITS,
}

#: env-file keys each restart-class setting is stored under. The units read these with
#: EnvironmentFile= and reference them in ExecStart, so writing the key and restarting is the
#: whole apply.
_ENV_KEY_FOR = {
    "led_brightness": "VS_LED_BRIGHTNESS",
    "wake_threshold": "VS_WAKE_THRESHOLD",
    "wake_word": "VS_WAKE_WORD",
}

_DEFAULT_ENV_FILE = Path(
    os.environ.get("VOICE_SATELLITE_ENV") or "/etc/voice-satellite/satellite.env"
)
_DEFAULT_MODELS_DIR = Path(os.environ.get("VS_WAKE_MODELS") or (Path.home() / "wake-models"))
_ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _write_env_key(path: Path, key: str, value: str) -> None:
    """Set KEY=VALUE in a systemd env file, adding it if absent, atomically.

    A missing file is written from scratch (a unit's EnvironmentFile= tolerates a missing
    file, and this device's is created by the deployer, so a fresh one is a legal state).
    Only the target key is changed; every other line, including comments, is preserved so the
    file is not rewritten out from under the units that read it.
    """
    lines = path.read_text().splitlines() if path.exists() else []
    replaced = False
    out = []
    for line in lines:
        if _ENV_LINE.match(line) and line.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(out) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_env_key(path: Path, key: str) -> Optional[str]:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if _ENV_LINE.match(line) and line.split("=", 1)[0].strip() == key:
            return line.split("=", 1)[1].strip()
    return None


def make_apply_adapter(
    volume: Optional[AlsaVolume] = None,
    mic_gain: Optional[AlsaMicGain] = None,
    env_path: Optional[Path] = None,
    wake_models_dir: Optional[Path] = None,
    systemctl: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> Callable[[dict[str, Any]], tuple[dict, str, Optional[str]]]:
    """Build the apply callback the configuration agent calls.

    Every dependency is injected so the tests can substitute a fake amixer, a fake env file and
    a fake `systemctl` - the production wiring (the `else` branches) is exercised by the
    runner-construction test instead, which is the only place that can catch a wiring gap.
    """
    alsa = volume or AlsaVolume.from_calibration()
    gain = mic_gain or AlsaMicGain.from_env()
    env_file = Path(env_path) if env_path is not None else _DEFAULT_ENV_FILE
    models_dir = Path(wake_models_dir) if wake_models_dir is not None else _DEFAULT_MODELS_DIR
    run_systemctl = systemctl or subprocess.run

    # What this process has already brought each restart-class setting to. The bridge re-sends
    # the full profile on every delivery, so "present in the profile" is not "changed": a
    # setting whose desired value equals what is already in force is skipped. On the first
    # touch of a setting this process has not handled, the env file is the fair authority for
    # what is in force: after a clean boot the service started with whatever the env file held.
    in_force: dict[str, Any] = {}
    # Settings whose env value this process wrote but whose restart FAILED. For those, the env
    # file is not the authority (the running service still holds the old value), so they must
    # not be treated as in force on redelivery: they stay pending a restart until one succeeds.
    stale: set[str] = set()

    def _wake_model_dir() -> Path:
        env_models_dir = _read_env_key(env_file, "VS_WAKE_MODELS")
        return Path(env_models_dir) if env_models_dir else models_dir

    def _is_in_force(name: str, desired: Any) -> bool:
        """Whether `name` is already at `desired` without any service being touched."""
        if name in stale:
            # This process wrote it but its restart failed: the running service still holds
            # the old value, so the env file is not authority here and it must be re-attempted.
            return False
        if in_force.get(name) == desired:
            return True
        if name not in in_force and _read_env_key(env_file, _ENV_KEY_FOR[name]) == str(desired):
            in_force[name] = desired
            return True
        return False

    def _restart(units: tuple[str, ...]) -> None:
        """Reload units and restart the named group, or raise. Never restarts the caller."""
        reload_res = run_systemctl(["systemctl", "daemon-reload"],
                                   capture_output=True, text=True, timeout=30, check=False)
        if reload_res.returncode != 0:
            raise RuntimeError(f"systemctl daemon-reload failed: "
                               f"{(reload_res.stderr or '').strip()[:160]}")
        for unit in units:
            if unit == "outbound-satellite":
                raise RuntimeError("refusing to restart outbound-satellite: it is the process "
                                   "applying this change")
        res = run_systemctl(["systemctl", "restart", *units],
                            capture_output=True, text=True, timeout=60, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"systemctl restart {' '.join(units)} failed: "
                               f"{(res.stderr or '').strip()[:160]}")

    def apply(settings: dict[str, Any]) -> tuple[dict, str, Optional[str]]:
        applied: dict[str, Any] = {}
        notes: list[str] = []             # applied-but-adjusted (the volume floor)
        hard: list[str] = []              # a path existed but failed -> error
        not_applicable: list[str] = []    # accepted, no path on this hardware -> degraded
        floor_adjusted = False
        units_to_restart: set[str] = set()
        pending: dict[str, Any] = {}      # restart-class values awaiting a successful restart

        # -- immediate: playback volume (output) -----------------------------------
        if "playback_volume" in settings:
            # The setting arrives on the USER scale (1-100), where 1 is the quietest this
            # hardware can be. AlsaVolume works in RAW, which is what the ALSA control takes,
            # so the conversion happens here and nowhere else on this side.
            requested = settings["playback_volume"]
            target_raw = alsa.user_to_raw(requested)
            current_raw = alsa.read()
            if current_raw is not None and current_raw == target_raw:
                # The hardware is already at this level (the full profile re-delivers it):
                # nothing to write, report what is in force.
                applied["playback_volume"] = alsa.raw_to_user(current_raw)
            else:
                try:
                    applied_raw = alsa.apply(target_raw)
                except VolumeCommandError as exc:
                    hard.append(f"playback_volume: {exc}")
                else:
                    # Report back on the user scale, so the number means the same thing on the
                    # bridge as it does on the device and convergence compares like with like.
                    applied_user = alsa.raw_to_user(applied_raw)
                    applied["playback_volume"] = applied_user
                    if applied_user != requested:
                        # The floor raised the requested level. It WAS applied (a real,
                        # audible level), so this stays healthy and the host's round-trip
                        # tolerance treats desired/requested as converged - the note is an
                        # explanation, not a degradation.
                        log.warning(
                            "playback_volume %s was raised to %s by the floor (raw %s)",
                            requested, applied_user, applied_raw)
                        notes.append(
                            f"playback_volume was raised from {requested} to {applied_user} by "
                            "the audible floor; the device must stay audible")
                        floor_adjusted = True

        # -- immediate: microphone input gain --------------------------------------
        if "mic_gain" in settings:
            if not gain.has_control():
                # No input control is configured for this card. This is a legitimate state
                # (capture may be an arecord command line), not a failure: accept the value,
                # report it as not applied, and name the reason rather than invent one.
                not_applicable.append(
                    "mic_gain: accepted but not applied - no microphone input control "
                    "configured on this device")
            else:
                target = max(0, min(100, int(settings["mic_gain"])))
                current = gain.read()
                if current is not None and current == target:
                    # Already at this level: nothing to do.
                    applied["mic_gain"] = current
                else:
                    try:
                        # set() reads the level back from the hardware and returns the value
                        # it confirmed, never the requested number: a control that quantizes
                        # or refuses is surfaced, not silently reported as applied.
                        applied["mic_gain"] = gain.set(target)
                    except VolumeGainError as exc:
                        hard.append(f"mic_gain: {exc}")

        # -- restart-class: env-file writes, one coupled restart at the end --------
        for name in ("led_brightness", "wake_threshold", "wake_word"):
            if name not in settings:
                continue
            value = settings[name]

            if _is_in_force(name, value):
                # Already in force: the full profile re-delivers unchanged settings on every
                # write, and acting on their mere presence is what would restart the audio
                # stack on a volume-only change. Report it as applied (it is) and move on.
                applied[name] = value
                continue

            if name == "wake_word":
                # The wake word is a TFLite model file, not a number. If its model is not on
                # this device the word cannot take effect, and writing it into the active
                # config would replace the wake word that currently works with one that cannot
                # load. So a missing model is refused up front: the working config is left
                # untouched, the value is reported degraded and named, and - because it never
                # enters `applied` - it stays retryable once the model is deployed.
                model_dir = _wake_model_dir()
                if not (model_dir / f"{value}.tflite").is_file():
                    not_applicable.append(
                        f"wake_word: {value} recorded but {value}.tflite is not present in "
                        f"{model_dir}; deploy the model file for it to take effect "
                        "(see docs/switching-wake-words.md)")
                    continue

            key = _ENV_KEY_FOR[name]
            try:
                _write_env_key(env_file, key, str(value))
            except OSError as exc:
                hard.append(f"{name}: could not write {key} to {env_file}: {exc}")
                continue
            # Read back to prove the write landed, for the same reason as the plane's own
            # persistence: a value the disk never saw must not be reported as applied.
            if _read_env_key(env_file, key) != str(value):
                hard.append(f"{name}: write to {key} did not land; not reporting it as applied")
                continue
            units_to_restart.update(_UNITS_FOR[name])
            # Held until the restart below succeeds: a value the service has not been restarted
            # with is not applied, even though the env file now says otherwise.
            pending[name] = value

        if units_to_restart:
            try:
                _restart(tuple(sorted(units_to_restart)))
            except RuntimeError as exc:
                hard.append(str(exc))
                # A failed restart means the env file says one thing and the running services
                # another. The value is still durable (it takes effect at the next restart),
                # so it is NOT reported as applied: it stays retryable, and the report is an
                # error, not convergence. Mark it stale so a redelivery re-attempts the restart
                # rather than trusting the env file.
                stale.update(pending)
            else:
                for name, value in pending.items():
                    applied[name] = value
                    in_force[name] = value
                stale.difference_update(pending)

        if hard:
            # A path existed and failed. Whatever was also applied is real, but the report
            # must not claim convergence.
            return applied, "error", "; ".join(hard)
        if not_applicable:
            # A setting was accepted but this hardware has no path for it (e.g. no input
            # control for mic_gain, or a wake word whose model is missing). Honest: the value
            # is recorded and will apply if a path appears, but it is not in force, so this is
            # degraded, not healthy.
            messages = not_applicable + (notes if floor_adjusted else [])
            return applied, "degraded", "; ".join(messages)
        if floor_adjusted:
            # Only a floor adjustment: the level was actually set and is audible, so this is
            # healthy, and the note explains the one-step correction.
            return applied, "healthy", "; ".join(notes)
        if not applied:
            return {}, "healthy", None
        return applied, "healthy", None

    return apply


__all__ = [
    "COUPLED_AUDIO_UNITS", "make_apply_adapter",
    "_write_env_key", "_read_env_key",
]
