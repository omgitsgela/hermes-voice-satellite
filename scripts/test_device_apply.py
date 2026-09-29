#!/usr/bin/env python3
"""Device-side apply adapter checks (issues #10 / #6).

The adapter is what turns desired settings into hardware change. The property that
matters most here is honesty: it must never report a setting as applied when it was not,
and it must never report a level it did not set.

Every hardware dependency is faked so the test runs on the bridge host with no Pi: a fake
`amixer`, a fake env file on disk, a fake `systemctl`, and a fake mic-gain control.
"""
from __future__ import annotations

import asyncio
import json as _json
import pathlib
import subprocess
import sys
import tempfile
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pi.device_apply as _device_apply  # noqa: E402
from pi.device_apply import make_apply_adapter, COUPLED_AUDIO_UNITS  # noqa: E402
from pi.volume_control import AlsaVolume  # noqa: E402
from pi.mic_gain_control import AlsaMicGain  # noqa: E402
from wyoming.event import Event  # noqa: E402
from pi.config_agent import DeviceConfigAgent  # noqa: E402

problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


class FakeAmixer:
    """Stands in for `amixer` for the OUTPUT control (volume).

    Tracks the raw level it was set to, so the adapter's "already at this level" read path
    (the point of the full-profile idempotency fix) is exercised honestly: `sget` reports the
    stored level and `sset` updates it.
    """
    def __init__(self, fail_set: bool = False, start_level: Optional[int] = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_set = fail_set
        self.level: Optional[int] = start_level

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        r = R()
        if "sget" in argv and self.level is not None:
            r.stdout = (f"Simple mixer control 'PCM',0\n  Capabilities: volume\n"
                        f"  Playback: [{self.level}]%\n")
        if "sset" in argv and self.fail_set:
            r.returncode = 1
            r.stderr = "amixer: unable to set"
        if "sset" in argv and not self.fail_set:
            for part in argv:
                if part.endswith("%"):
                    self.level = int(part.rstrip("%"))
        return r


class FakeMicAmixer:
    """Stands in for `amixer` for the INPUT control; tracks the level it was set to, so the
    readback path (the point of the fix) is exercised honestly."""
    def __init__(self, present: bool = True, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.present = present
        self.fail = fail
        self.level: Optional[int] = None
        self._refuse_readback = False

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        r = R()
        if "sget" in argv and not self.present:
            r.returncode = 1
            r.stderr = "amixer: control Mic not found"
        if "sget" in argv and self.present and not self._refuse_readback:
            r.stdout = (f"Simple mixer control 'Mic',0\n  Capabilities: volume\n  Playback: "
                        f"[{self.level if self.level is not None else 0}%]\n")
        if "sset" in argv and self.fail:
            r.returncode = 1
            r.stderr = "amixer: unable to write"
        if "sset" in argv and not self.fail and self.present:
            for part in argv:
                if part.endswith("%"):
                    self.level = int(part.rstrip("%"))
        return r


class FakeSystemctl:
    """Stands in for `systemctl`; records restarts, can be made to fail.

    `fail_mode` selects HOW the restart fails: "rc" returns a nonzero exit code;
    "timeout" raises subprocess.TimeoutExpired; "oserror" raises OSError. All three must
    be treated by the adapter as a failed restart (the value stays out of actual, the
    report is an error, and it is retryable) - that is the point of the regression.
    """
    def __init__(self, fail_restart: bool = False, fail_mode: str = "rc") -> None:
        self.calls: list[list[str]] = []
        self.fail_restart = fail_restart
        self.fail_mode = fail_mode

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        if self.fail_restart and "restart" in argv:
            if self.fail_mode == "timeout":
                raise subprocess.TimeoutExpired(cmd="systemctl", timeout=60)
            if self.fail_mode == "oserror":
                raise OSError("systemctl: no such file or directory")
        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        r = R()
        if self.fail_restart and "restart" in argv and self.fail_mode == "rc":
            r.returncode = 1
            r.stderr = "systemctl: failed to restart"
        return r


def make_env(tmp) -> pathlib.Path:
    env = pathlib.Path(tmp) / "satellite.env"
    env.write_text("VS_DEVICE_NAME=kitchen\nVS_WAKE_WORD=hey_hermes\nVS_WAKE_THRESHOLD=0.35\n")
    return env


# ---------------------------------------------------------------- volume applies

fake = FakeAmixer()
adapter = make_apply_adapter(volume=AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake))
actual, health, error = adapter({"playback_volume": 70})
check("a valid volume is applied", actual.get("playback_volume") == 70, actual)
check("applying a valid volume is healthy", health == "healthy", health)
check("applying a valid volume reports no problem", error is None, error)
check("the device was actually told", any("sset" in c for c in fake.calls), fake.calls)
check("the level was persisted for reboot survival",
      any("alsactl" in c[0] for c in fake.calls), fake.calls)

# ---------------------------------------------------------------- the floor is surfaced

fake = FakeAmixer()
adapter = make_apply_adapter(volume=AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake))
actual, health, error = adapter({"playback_volume": 0})
# The setting is on the USER scale, so zero is refused up to user 1 - the quietest this
# hardware can be - and user 1 maps to the raw floor of 25. It WAS applied (a real, audible
# level), so it stays healthy; the correction is an explanation, not a degradation.
check("a zero request is reported as the quietest USER level",
      actual.get("playback_volume") == 1, actual)
check("a zero request reaches amixer as the raw floor, never 0%",
      any("25%" in part for c in fake.calls for part in c)
      and not any("0%" in part for c in fake.calls for part in c), fake.calls)
check("the floor adjustment is explained, not silent",
      error is not None and "raised" in error, error)
check("the report says what it was raised from and to, on the user scale",
      "from 0 to 1" in error, error)
check("the floor-adjusted level is healthy, because it was applied",
      health == "healthy", health)

# ---------------------------------------------------------------- a failure is not a success

fake = FakeAmixer(fail_set=True)
adapter = make_apply_adapter(volume=AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=fake))
actual, health, error = adapter({"playback_volume": 60})
check("a failed volume set reports no applied level", "playback_volume" not in actual, actual)
check("a failed volume set is not healthy", health != "healthy", health)
check("a failed volume set explains itself", error is not None and "playback_volume" in error, error)

# ---------------------------------------------------------------- mic gain: applied

with tempfile.TemporaryDirectory() as tmp:
    mic = FakeMicAmixer(present=True)
    env = make_env(tmp)
    adapter = make_apply_adapter(
        volume=AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=FakeAmixer()),
        mic_gain=AlsaMicGain(card="seeed2micvoicec", control="Mic", runner=mic),
        env_path=env, systemctl=FakeSystemctl(),
    )
    actual, health, error = adapter({"mic_gain": 60})
    check("a mic gain with a control is applied", actual.get("mic_gain") == 60, actual)
    check("applying a real mic gain is healthy", health == "healthy", health)
    check("the input control was actually set",
          any("sset" in c and "60%" in c for c in mic.calls), mic.calls)

# ---------------------------------------------------------------- mic gain: no control -> degraded, not applied

with tempfile.TemporaryDirectory() as tmp:
    mic = FakeMicAmixer(present=False)
    env = make_env(tmp)
    adapter = make_apply_adapter(
        volume=AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=FakeAmixer()),
        mic_gain=AlsaMicGain(card="seeed2micvoicec", control="", runner=mic),
        env_path=env, systemctl=FakeSystemctl(),
    )
    actual, health, error = adapter({"mic_gain": 60})
    check("a mic gain with no input control is NOT reported as applied",
          actual.get("mic_gain") is None, actual)
    check("a mic gain with no input control is degraded, not healthy",
          health == "degraded", health)
    check("the missing-control state is named",
          error is not None and "mic_gain" in error and "not applied" in error, error)
    check("no amixer set was attempted for a control that is absent",
          not any("sset" in c for c in mic.calls), mic.calls)

# ---------------------------------------------------------------- mic gain: set fails -> error

with tempfile.TemporaryDirectory() as tmp:
    mic = FakeMicAmixer(present=True, fail=True)
    env = make_env(tmp)
    adapter = make_apply_adapter(
        mic_gain=AlsaMicGain(card="seeed2micvoicec", control="Mic", runner=mic),
        env_path=env, systemctl=FakeSystemctl(),
    )
    actual, health, error = adapter({"mic_gain": 60})
    check("a mic gain whose set fails is reported as an error, not applied",
          health == "error" and actual.get("mic_gain") is None, (health, actual))

# ---------------------------------------------------------------- led brightness: env + restart

with tempfile.TemporaryDirectory() as tmp:
    env = make_env(tmp)
    sc = FakeSystemctl()
    adapter = make_apply_adapter(
        env_path=env, systemctl=sc,
        wake_models_dir=pathlib.Path(tmp),
    )
    actual, health, error = adapter({"led_brightness": 18})
    check("led brightness is applied and reported", actual.get("led_brightness") == 18, actual)
    check("a clean led change is healthy", health == "healthy", health)
    check("the led value was written to the env file",
          any("VS_LED_BRIGHTNESS=18" in line for line in env.read_text().splitlines()),
          env.read_text())
    # The LED service is part of the coupled audio group (wyoming-satellite Requires= and
    # After= it), so an "LED-only" restart is not an independent operation: the whole group
    # goes, one group, or a unit is left dead. outbound-satellite is the one unit that must
    # stay up (it is the process reporting the result).
    check("a led change restarts the whole coupled audio group",
          any(c[0] == "systemctl" and c[1] == "restart"
              and set(c[2:]) == set(COUPLED_AUDIO_UNITS) for c in sc.calls), sc.calls)
    check("a led change never restarts outbound-satellite (the applying process)",
          not any("outbound-satellite" in c for c in sc.calls), sc.calls)
    check("the units were reloaded before the restart",
          any(c == ["systemctl", "daemon-reload"] for c in sc.calls), sc.calls)

# ---------------------------------------------------------------- wake threshold: env + full coupled restart

with tempfile.TemporaryDirectory() as tmp:
    env = make_env(tmp)
    sc = FakeSystemctl()
    adapter = make_apply_adapter(env_path=env, systemctl=sc)
    actual, health, error = adapter({"wake_threshold": 0.4})
    check("wake threshold is applied and reported", actual.get("wake_threshold") == 0.4, actual)
    check("a clean wake-threshold change is healthy", health == "healthy", health)
    check("the threshold was written to the env file",
          any("VS_WAKE_THRESHOLD=0.4" in line for line in env.read_text().splitlines()),
          env.read_text())
    check("a wake change restarts the whole coupled audio group",
          any(c[0] == "systemctl" and c[1] == "restart"
              and set(c[2:]) == {"wyoming-satellite", "wyoming-openwakeword",
                                 "wake-shim", "2mic_leds"} for c in sc.calls), sc.calls)
    check("a wake change never restarts outbound-satellite (the applying process)",
          not any("outbound-satellite" in c for c in sc.calls), sc.calls)
    check("the units were reloaded before the restart",
          any(c == ["systemctl", "daemon-reload"] for c in sc.calls), sc.calls)

# ---------------------------------------------------------------- wake word: env + restart, model check is honest

with tempfile.TemporaryDirectory() as tmp:
    env = make_env(tmp)
    models = pathlib.Path(tmp) / "models"
    models.mkdir()
    (models / "hey_hermes.tflite").write_bytes(b"\x00")
    sc = FakeSystemctl()
    adapter = make_apply_adapter(env_path=env, systemctl=sc, wake_models_dir=models)
    # A word whose model is present: applied, healthy.
    actual, health, error = adapter({"wake_word": "hey_hermes"})
    check("a wake word with its model present is applied", actual.get("wake_word") == "hey_hermes", actual)
    check("a wake word with its model present is healthy", health == "healthy", health)

    # A word whose model is absent: refused up front - the working config is left untouched,
    # the value is reported degraded and named, and it is NOT reported as applied (so it stays
    # retryable once the model is deployed).
    (models / "hey_hermes.tflite").unlink()
    actual, health, error = adapter({"wake_word": "hey_karen"})
    check("a wake word whose model is missing is NOT reported as applied",
          actual.get("wake_word") is None, actual)
    check("a wake word whose model is missing is degraded, not healthy",
          health == "degraded", health)
    check("the missing model is named",
          error is not None and "hey_karen.tflite" in error, error)
    check("the missing-model word was not written into the active config",
          env.read_text().count("VS_WAKE_WORD=hey_karen") == 0, env.read_text())
    check("a missing-model word did not restart the wake stack",
          not any("restart" in c for c in sc.calls), sc.calls)

# ---------------------------------------------------------------- a failed restart is reported, not converged

with tempfile.TemporaryDirectory() as tmp:
    env = make_env(tmp)
    sc = FakeSystemctl(fail_restart=True)
    adapter = make_apply_adapter(env_path=env, systemctl=sc)
    actual, health, error = adapter({"led_brightness": 18})
    check("a failed restart is reported as an error, not converged",
          health == "error" and "restart" in (error or ""), (health, error))
    check("a failed restart does NOT report the value as applied (it stays retryable)",
          actual.get("led_brightness") is None, actual)
    check("the value was still written durably before the failed restart",
          any("VS_LED_BRIGHTNESS=18" in line for line in env.read_text().splitlines()),
          env.read_text())
    # Within the same process, redelivering the same generation must retry the restart, not
    # trust the env file and skip it: a stale value is re-attempted until a restart succeeds.
    sc.fail_restart = False
    actual2, health2, error2 = adapter({"led_brightness": 18})
    check("a redelivery after a failed restart re-attempts it and converges",
          health2 == "healthy" and actual2.get("led_brightness") == 18, (health2, actual2))
    check("the retry actually restarted the coupled audio group",
          any(c[0] == "systemctl" and c[1] == "restart"
              and set(c[2:]) == set(COUPLED_AUDIO_UNITS) for c in sc.calls), sc.calls)

# ---------------------------------------------------------------- failed restart survives adapter recreation (review F2)

# The env file is durable; the in-memory "this restart failed" knowledge was not. If the
# outbound runner (not the audio stack) is restarted after a failed restart, a FRESH adapter
# reads the env file, sees the staged value, and - without being told the restart failed -
# would report it healthy without ever touching systemctl. That is a converged flag that is
# a lie. The fix: the previous adapter's unapplied set is seeded into the new adapter, so
# the env file is not authority for those values.
with tempfile.TemporaryDirectory() as tmp:
    env = make_env(tmp)
    sc_failed = FakeSystemctl(fail_restart=True)
    adapter1 = make_apply_adapter(env_path=env, systemctl=sc_failed)
    actual, health, error = adapter1({"led_brightness": 18})
    check("the failed first attempt is not reported as applied",
          health == "error" and actual.get("led_brightness") is None, (health, actual))
    check("the failed value was written durably to the env file",
          any("VS_LED_BRIGHTNESS=18" in line for line in env.read_text().splitlines()),
          env.read_text())
    # The audio services still run the OLD value (the restart failed); only the env file
    # moved. A fresh adapter (the outbound runner restarted) must NOT trust the env file
    # for this value: no stale seed -> it would read the env and skip the restart, which is
    # exactly the false success we are guarding against.
    sc_fresh_bad = FakeSystemctl()
    adapter_fresh = make_apply_adapter(env_path=env, systemctl=sc_fresh_bad)
    bad_actual, bad_health, _ = adapter_fresh({"led_brightness": 18})
    check("an unseeded fresh adapter WOULD trust the env file (the defect the seed prevents)",
          bad_health == "healthy" and bad_actual.get("led_brightness") == 18
          and not any("restart" in c for c in sc_fresh_bad.calls),
          (bad_health, bad_actual, sc_fresh_bad.calls))
    # With the stale seed (what the agent persists and the runner hands over), the same
    # fresh adapter re-attempts the restart instead of reporting a lie.
    sc_fresh = FakeSystemctl()
    adapter_seeded = make_apply_adapter(
        env_path=env, systemctl=sc_fresh, stale_seed=["led_brightness"])
    seeded_actual, seeded_health, seeded_error = adapter_seeded({"led_brightness": 18})
    check("a seeded fresh adapter re-attempts the restart, does not trust the env file",
          seeded_health == "healthy" and seeded_actual.get("led_brightness") == 18,
          (seeded_health, seeded_actual, seeded_error))
    check("the seeded re-attempt actually restarted the coupled group",
          any(c[0] == "systemctl" and c[1] == "restart"
              and set(c[2:]) == set(COUPLED_AUDIO_UNITS) for c in sc_fresh.calls), sc_fresh.calls)

# ---------------------------------------------------------------- subprocess failures are failed restarts (third-pass F2)

# A systemctl that TIMES OUT or cannot be RUN raises subprocess.TimeoutExpired / OSError,
# not a nonzero exit code. Those are restart failures just like a bad exit code: the value
# must stay out of actual, the report must be an error, and the setting must stay stale so
# a redelivery re-attempts the restart instead of trusting the env file and reporting a
# value that was never restarted into force.
for mode, label in (("timeout", "a systemctl timeout"), ("oserror", "an OSError from systemctl")):
    with tempfile.TemporaryDirectory() as tmp:
        env = make_env(tmp)
        sc = FakeSystemctl(fail_restart=True, fail_mode=mode)
        adapter = make_apply_adapter(env_path=env, systemctl=sc)
        actual, health, error = adapter({"led_brightness": 18})
        check(f"{label} is reported as an error, not converged",
              health == "error" and error is not None, (health, error))
        check(f"{label} does NOT report the value as applied (it stays retryable)",
              actual.get("led_brightness") is None, actual)
        check(f"{label} still wrote the value durably",
              any("VS_LED_BRIGHTNESS=18" in line for line in env.read_text().splitlines()),
              env.read_text())
        # Redelivery within the same process must re-attempt the restart, not trust the
        # env file: once the failure clears, the value converges.
        sc.fail_restart = False
        actual2, health2, error2 = adapter({"led_brightness": 18})
        check(f"{label}: a redelivery after the failure clears re-attempts and converges",
              health2 == "healthy" and actual2.get("led_brightness") == 18, (health2, actual2))

# ---------------------------------------------------------------- a failed readback is a failed apply (fourth-pass F1)

# A transient I/O error on the POST-WRITE env readback is a failure, not a proof: the value
# was written to the file but never restarted into force. Before the fix the raw OSError
# escaped apply(), the agent's error path saved no state, and a redelivery read the env
# file and reported the value healthy with zero systemctl calls - a converged flag that is a
# lie. The value must be marked stale the moment it is written, the readback failure must be
# reported (not raised), and a redelivery must re-attempt the restart.
def _state_pending_restart(state_path):
    """The persisted pending_restart list, or a sentinel if the state file was never
    written (the defect: the agent's error path saves no state at all)."""
    try:
        return _json.loads(state_path.read_text()).get("pending_restart")
    except (FileNotFoundError, ValueError):
        return "<state not persisted>"


def _flaky_env_reader(real_read):
    """A real env reader that fails the FIRST read of a key whose value is present -
    exactly the post-write readback, not the pre-write "is it in force" read (key absent)."""
    state = {"n": 0}
    def flaky(path, key):
        val = real_read(path, key)
        if val is not None and state["n"] == 0:
            state["n"] += 1
            raise OSError("simulated transient I/O error reading the env file")
        return val
    return flaky

async def readback_failure_regression() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = make_env(tmp)
        sc = FakeSystemctl()
        state_path = pathlib.Path(tmp) / "config.json"
        real_read = _device_apply._read_env_key
        _device_apply._read_env_key = _flaky_env_reader(real_read)
        try:
            agent = DeviceConfigAgent(
                state_path,
                apply=make_apply_adapter(env_path=env, systemctl=sc),
                device_id="kitchen-1",
            )
            report = await agent.apply_event(Event(type="hermes.config.desired", data={
                "schema_version": 1, "device_id": "kitchen-1", "generation": 1,
                "settings": {"led_brightness": 18},
            }))
            check("a failed post-write readback is reported as an error, not applied",
                  report.data["health"] == "error"
                  and report.data["actual"].get("led_brightness") is None, report.data)
            check("the readback failure is named in the report",
                  report.data["error"] is not None and "read back" in report.data["error"],
                  report.data)
            check("the failed readback raised no exception through the agent",
                  report.data["health"] != "unknown", report.data)
            check("the value was still written durably to the env file",
                  any("VS_LED_BRIGHTNESS=18" in line for line in env.read_text().splitlines()),
                  env.read_text())
            check("the unapplied value is persisted as a pending restart (a recreated runner must not trust the env file)",
                  _state_pending_restart(state_path) == ["led_brightness"],
                  str(_state_pending_restart(state_path)))
            check("no restart was attempted before the readback could be proven",
                  not any("restart" in c for c in sc.calls), sc.calls)
            # Redelivery within the same process: the adapter must NOT trust the env file
            # for this value - it must re-attempt the restart instead of reporting a value
            # that was never restarted into force as healthy.
            report2 = await agent.apply_event(Event(type="hermes.config.desired", data={
                "schema_version": 1, "device_id": "kitchen-1", "generation": 1,
                "settings": {"led_brightness": 18},
            }))
            check("a redelivery after the readback failure re-attempts and converges",
                  report2.data["health"] == "healthy"
                  and report2.data["actual"].get("led_brightness") == 18, report2.data)
            check("the redelivery restarted the coupled audio group (not a silent env trust)",
                  any(c[0] == "systemctl" and c[1] == "restart"
                      and set(c[2:]) == set(COUPLED_AUDIO_UNITS) for c in sc.calls), sc.calls)
            check("the pending restart is cleared once the restart succeeds",
                  _state_pending_restart(state_path) == [],
                  str(_state_pending_restart(state_path)))
        finally:
            _device_apply._read_env_key = real_read

asyncio.run(readback_failure_regression())

# ---------------------------------------------------------------- env file: update existing key, preserve others

with tempfile.TemporaryDirectory() as tmp:
    env = make_env(tmp)
    sc = FakeSystemctl()
    adapter = make_apply_adapter(env_path=env, systemctl=sc)
    adapter({"wake_threshold": 0.4})
    text = env.read_text()
    check("an existing env key is updated in place, not duplicated",
          text.count("VS_WAKE_THRESHOLD") == 1, text)
    check("unrelated env keys are preserved",
          "VS_DEVICE_NAME=kitchen" in text and "VS_WAKE_WORD=hey_hermes" in text, text)

# ---------------------------------------------------------------- idempotency against the full profile (review F1)

# The bridge re-sends the complete merged profile on every delivery. A volume-only change
# must not bounce the LED or wake services just because those settings happen to be present
# (unchanged) in the profile - that is exactly what would restart the whole fleet's audio
# stack on every volume change.
with tempfile.TemporaryDirectory() as tmp:
    env = make_env(tmp)
    sc = FakeSystemctl()
    amixer = FakeAmixer(start_level=50)
    models = pathlib.Path(tmp) / "models"
    models.mkdir()
    (models / "hey_hermes.tflite").write_bytes(b"\x00")
    adapter = make_apply_adapter(
        volume=AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=amixer),
        env_path=env, systemctl=sc, wake_models_dir=models,
    )
    # First delivery establishes the restart-class values in force (one coupled restart each
    # is legitimate).
    first, h1, e1 = adapter({
        "playback_volume": 55, "led_brightness": 20, "wake_threshold": 0.4,
    })
    check("the first full-profile delivery applies and restarts as needed",
          h1 == "healthy" and first.get("led_brightness") == 20, (h1, first))
    restarts_before = sum(1 for c in sc.calls if c[:2] == ["systemctl", "restart"])
    # A volume-only change, delivered as the full profile again: LED and threshold are
    # unchanged, so nothing restart-class may be written or restarted.
    second, h2, e2 = adapter({
        "playback_volume": 65, "led_brightness": 20, "wake_threshold": 0.4,
    })
    restarts_after = sum(1 for c in sc.calls if c[:2] == ["systemctl", "restart"])
    check("a volume-only change is healthy", h2 == "healthy", (h2, e2))
    # user->raw->user round trips through an integer ALSA percentage and shifts by at most
    # 1 (the host's round_trip_tolerance of 1 accepts it), so assert the applied level is the
    # requested one within that one-step tolerance, not an exact integer match.
    check("the volume change is applied (within the round-trip tolerance)",
          second.get("playback_volume") is not None
          and abs(second["playback_volume"] - 65) <= 1, second)
    check("a volume-only change does NOT restart the audio stack",
          restarts_after == restarts_before,
          f"{restarts_before} -> {restarts_after}")
    check("an unchanged LED is reported as applied (it is) without a restart",
          second.get("led_brightness") == 20, second)

# ---------------------------------------------------------------- the agent end-to-end

async def agent_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = make_env(tmp)
        sc = FakeSystemctl()
        agent = DeviceConfigAgent(
            pathlib.Path(tmp) / "config.json",
            apply=make_apply_adapter(
                volume=AlsaVolume(card="seeed2micvoicec", control="PCM", floor=25, runner=FakeAmixer()),
                env_path=env, systemctl=sc,
            ),
            device_id="kitchen-1",
        )
        report = await agent.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1, "device_id": "kitchen-1", "generation": 1,
            "settings": {"playback_volume": 55},
        }))
        check("the agent reports a healthy volume change",
              report.data["health"] == "healthy", report.data)
        check("the agent reports the applied level, not the requested one",
              report.data["actual"].get("playback_volume") == 55, report.data)

        # A restart-class change through the whole chain: env written, group restarted.
        sc2 = FakeSystemctl()
        agent2 = DeviceConfigAgent(
            pathlib.Path(tmp) / "config2.json",
            apply=make_apply_adapter(env_path=env, systemctl=sc2),
            device_id="kitchen-1",
        )
        report = await agent2.apply_event(Event(type="hermes.config.desired", data={
            "schema_version": 1, "device_id": "kitchen-1", "generation": 2,
            "settings": {"led_brightness": 22},
        }))
        check("a restart-class change through the agent is healthy",
              report.data["health"] == "healthy", report.data)
        check("the agent reports the restart-class value it applied",
              report.data["actual"].get("led_brightness") == 22, report.data)
        check("the agent's restart-class change restarted the coupled audio group",
              any(c[0] == "systemctl" and c[1] == "restart"
                  and set(c[2:]) == set(COUPLED_AUDIO_UNITS) for c in sc2.calls), sc2.calls)


asyncio.run(agent_round_trip())

print(f"device apply checks: {checks - len(problems)}/{checks} passed")
for problem in problems:
    print(f"  FAIL: {problem}")
raise SystemExit(1 if problems else 0)
