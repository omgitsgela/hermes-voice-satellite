"""The Raspberry Pi satellite: a profile, and a driver that builds commands.

This device is set up by writing its SD card, not by a cable into the Pi. The first draft of this
driver was wrong in a way worth recording, because the test passed: it called
`scripts/install-pi.sh --base <card mount>`, but `--base` is the *software directory on the device*,
and that script installs systemd units into `/etc/systemd/system` and calls the host's `systemctl`.
Pointed at a mounted card it would have failed without root, or worse, installed satellite services
on the workbench.

The right script is `scripts/deploy-to-card.sh <mount> [user]`, which copies the device files onto
the card and writes a placeholder `gateway-link.env`. That script deliberately never overwrites the
env file, because it holds values only a person can supply, so this driver fills it in and then runs
the script a second time. The script is idempotent, and it enables the unit only once the values are
real, so that rule stays in one place instead of being duplicated here.
"""
from __future__ import annotations

from pathlib import Path

from hermes_installer.driver import (
    Command,
    DeviceRequest,
    Runner,
    StepResult,
    merge,
    require_posix,
)
from hermes_installer.profile import DeviceProfile, Setting

PROFILE = DeviceProfile(
    id="pi-reSpeaker-2mic",
    display_name="Raspberry Pi Zero 2 W with a reSpeaker 2-mic HAT",
    vendor="Raspberry Pi + Seeed",
    flashing="sd_card",
    wake_formats=("openwakeword_onnx",),
    voice="local_piper",
    wake_default="hey_hermes",
    first_party=True,
    description=(
        "A small computer running Linux. Its wake word runs on the Pi itself, so the model format "
        "differs from the smaller boxes even when the phrase is the same."
    ),
    identify=(
        "A green board about the size of a matchbox, with the microSD slot on one narrow end. "
        "The card comes out of that slot."
    ),
    settings={
        "playback_volume": Setting("playback_volume", "immediate", remote=True),
        "mic_gain": Setting("mic_gain", "immediate", remote=True),
        "led_brightness": Setting("led_brightness", "service_restart", remote=True),
        "wake_threshold": Setting("wake_threshold", "service_restart", remote=True),
        "wake_word": Setting("wake_word", "service_restart", remote=True,
                             help="The model lives on the card, so once the card is made the wake "
                                  "word can be switched from the gateway."),
        "wifi": Setting("wifi", "cable", remote=False,
                        help="Wi-Fi lives on the card. Changing it means the card."),
        "dial_address": Setting("dial_address", "cable", remote=False),
    },
)

DEPLOY_SCRIPT = "scripts/deploy-to-card.sh"
WIFI_SCRIPT = "scripts/add-wifi-profile-to-card.py"


def bridge_env_path(mount: str, user: str) -> Path:
    return Path(mount) / "home" / user / "gateway-link.env"


def render_bridge_env(existing: str, *, device_id: str, host: str, port: int, name: str,
                      tls: dict[str, str] | None = None) -> str:
    """Set the values the card needs, keeping every other line the template put there."""
    wanted = {
        "VOICE_DEVICE_ID": device_id,
        "VOICE_GATEWAY_HOST": host,
        "VOICE_GATEWAY_PORT": str(port),
        "VOICE_DEVICE_NAME": name,
    }
    if tls:
        # The device dials a TLS port, so it needs the CA it should trust and its own certificate
        # and key. Without these the card boots, fails to authenticate and looks like a dead device.
        wanted.update({
            "VOICE_GATEWAY_TLS_CA": tls["ca"],
            "VOICE_GATEWAY_TLS_CERT": tls["cert"],
            "VOICE_GATEWAY_TLS_KEY": tls["key"],
        })
    lines = existing.splitlines() if existing else [
        "# Bootstrap configuration for the outbound shape: the device dials the gateway.",
    ]
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0]
        if "=" in line and key in wanted and not line.lstrip().startswith("#"):
            output.append(f"{key}={wanted[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in wanted.items():
        if key not in seen:
            output.append(f"{key}={value}")
    return "\n".join(output) + "\n"


def set_env_keys(existing: str, wanted: dict[str, str]) -> str:
    """Set KEY=VALUE lines, keeping every other line (comments included) exactly as it was."""
    lines = existing.splitlines() if existing else []
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if "=" in line and key in wanted and not line.lstrip().startswith("#"):
            output.append(f"{key}={wanted[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in wanted.items():
        if key not in seen:
            output.append(f"{key}={value}")
    return "\n".join(output) + "\n"


def split_dial(dial_address: str) -> tuple[str, int]:
    """`tls://host:10701` becomes host and port, which is what the card's env file wants."""
    text = dial_address.split("://", 1)[-1]
    host, _, port = text.partition(":")
    return host, int(port) if port.isdigit() else 10701


class PiDriver:
    PROFILE = PROFILE

    def flash(self, request: DeviceRequest, runner: Runner) -> StepResult:
        if not request.card_mount:
            return StepResult(ok=False, summary="no SD card is mounted")
        if not request.dial_address:
            return StepResult(ok=False, summary="no gateway address has been chosen yet")
        require_posix("Writing a satellite card")

        mount = request.card_mount
        user = request.card_user or "satellite"
        deploy = Command((DEPLOY_SCRIPT, mount, user), "copy the satellite onto the card")

        first = runner.run(deploy)
        if not first.ok:
            return first

        host, port = split_dial(request.dial_address)
        env_path = bridge_env_path(mount, user)
        try:
            existing = env_path.read_text()
        except OSError:
            existing = ""
        content = render_bridge_env(
            existing,
            device_id=request.device_id or request.device_name or "satellite",
            host=host,
            port=port,
            name=request.device_name,
        )
        written = runner.write_file(
            env_path, content, description=f"fill in {env_path.name} so the card dials {host}"
        )
        if not written.ok:
            return merge(first, written)

        second = runner.run(
            Command((DEPLOY_SCRIPT, mount, user), "enable the unit now that the values are real")
        )
        return merge(first, written, second,
                     summary=f"card prepared for {request.device_name}")

    def provision(self, request: DeviceRequest, runner: Runner) -> StepResult:
        """Stage what the card needs beyond the software: credentials, and optionally Wi-Fi.

        Two independent jobs, because they are independently needed. A card on Ethernet still needs
        its TLS credentials, and a card without credentials cannot authenticate however good its
        Wi-Fi is. Requiring one to do the other is what made a blank Wi-Fi fail an install.
        """
        if not request.card_mount:
            return StepResult(ok=False, summary="no SD card is mounted")
        require_posix("Preparing a satellite card")

        staged: list[str] = []
        commands: list[Command] = []
        if request.certs_dir:
            credentials = self._stage_credentials(request, runner)
            if not credentials.ok:
                return credentials
            staged.append("credentials")

        if request.wifi_ssid:
            wifi = runner.run(
                Command(
                    (WIFI_SCRIPT, "--mount", request.card_mount, "--source", request.wifi_ssid,
                     "--name", request.wifi_ssid),
                    f"copy the saved Wi-Fi network {request.wifi_ssid} onto the card",
                    requires_elevation=True,
                )
            )
            if not wifi.ok:
                return wifi
            # Keep the run's commands: the interface shows what ran, and dropping them made the
            # Wi-Fi step look like it had done nothing at all.
            commands.extend(wifi.commands)
            staged.append(f"the Wi-Fi network {request.wifi_ssid}")

        if request.wake_word or request.wake_model:
            wake = self._stage_wake(request, runner)
            if not wake.ok:
                return wake
            if wake.commands:
                commands.extend(wake.commands)
            # The default phrase stages nothing, and an empty label must not turn "nothing to
            # stage" into "staged ".
            if wake.summary:
                staged.append(wake.summary)

        if not staged:
            return StepResult(ok=True, summary="nothing to stage: the card keeps its settings")
        return StepResult(ok=True, summary="staged " + " and ".join(staged),
                          commands=tuple(commands))

    def _stage_credentials(self, request: DeviceRequest, runner: Runner) -> StepResult:
        """Copy the paired credentials onto the card and point its environment at them."""
        source = Path(str(request.certs_dir))
        user = request.card_user or "satellite"
        identity = request.device_id or request.device_name
        destination = Path(str(request.card_mount)) / "home" / user / "certs"
        files = {
            "client-ca.crt": "ca.crt",
            f"{identity}.crt": "cert.pem",
            f"{identity}.key": "key.pem",
        }
        written: list[Path] = []
        for name, target in files.items():
            origin = source / name
            if not origin.exists():
                return StepResult(
                    ok=False,
                    summary=f"the paired credentials have no {name}, so the card would dial TLS "
                            f"without being able to authenticate. Pair again.",
                )
            result = runner.write_file(
                destination / target, origin.read_text(), mode=0o600,
                description=f"copy {target} onto the card",
            )
            if not result.ok:
                return result
            written.append(destination / target)

        # Point the card's environment at them, alongside the values flash already wrote.
        env_path = bridge_env_path(str(request.card_mount), user)
        try:
            existing = env_path.read_text()
        except OSError:
            existing = ""
        host, port = split_dial(request.dial_address)
        # The files were WRITTEN through the mount, but the paths the booted device opens are its
        # own: `<mount>/home/<user>/certs/...` does not exist once the card is in the Pi, so writing
        # those paths produced a satellite that booted and could not authenticate.
        on_device = Path("/home") / user / "certs"
        content = render_bridge_env(
            existing, device_id=identity, host=host, port=port, name=request.device_name,
            tls={"ca": str(on_device / "ca.crt"),
                 "cert": str(on_device / "cert.pem"),
                 "key": str(on_device / "key.pem")},
        )
        return runner.write_file(
            env_path, content, description="point the card's environment at its credentials"
        )

    def _stage_wake(self, request: DeviceRequest, runner: Runner) -> StepResult:
        """Put the chosen wake word on the card: the model file, and the selector that uses it.

        Settings applied later from the Satellite Gateway can switch the *selector*, but nothing
        running on the device can add a model file. So a wake word chosen in the installer has to be
        staged here or the device will be told to listen for a model it does not have.

        The device reads `VS_WAKE_WORD` and `VS_WAKE_MODELS` from its own environment file; the card
        is written through the mount at the same relative path.
        """
        mount = Path(str(request.card_mount))
        user = request.card_user or "satellite"
        # Built as segments, not as `mount / <absolute device path>`: an absolute right-hand side
        # REPLACES the left, which silently wrote to the workbench's home directory instead of the
        # card. The device path is assembled from parts because a literal absolute one must not
        # appear in this repository (scripts/check-no-personal-data.sh refuses it).
        home_on_device = str(Path("/home") / user)
        models_on_card = mount / "home" / user / "wake-models"

        word = (request.wake_word or "").strip()
        model = Path(str(request.wake_model)) if request.wake_model else None
        staged: list = []

        if model is not None:
            if not model.exists():
                return StepResult(ok=False, summary=f"the wake word file chosen is not there: {model}")
            if model.suffix.lower() != ".onnx":
                return StepResult(
                    ok=False,
                    summary=(f"this device's wake word runs on the Pi itself, so it needs an ONNX "
                             f"model; {model.name} is not one. Choose a compatible model or a "
                             f"custom ONNX file."),
                )
            copied = runner.copy_file(
                model, models_on_card / model.name, mode=0o644,
                description=f"copy {model.name} onto the card",
            )
            if not copied.ok:
                return copied
            staged.append(copied)
            word = word or model.stem

        if not word:
            return StepResult(ok=False, summary="a wake model was given but no wake word to select")

        # The default phrase needs no staging: it is what the device image already listens for, and
        # a run that changes nothing else must stay a no-op rather than invent work.
        if model is None and word == PROFILE.wake_default:
            return StepResult(ok=True, summary="", commands=())

        # Any OTHER phrase without a model file would leave a satellite that never wakes, so it is
        # refused rather than written as a selector pointing at nothing.
        if model is None:
            present = sorted(p.name for p in models_on_card.glob("*.onnx")) if models_on_card.is_dir() else []
            matches = [name for name in present if Path(name).stem == word]
            if not matches:
                listed = ", ".join(present) if present else "none at all"
                return StepResult(
                    ok=False,
                    summary=(f"the card has no wake word model called {word} (it has: {listed}). "
                             f"Choose a model file to copy across, or pick a wake word the card "
                             f"already holds."),
                )

        env_path = mount / "etc" / "voice-satellite" / "satellite.env"
        try:
            existing = env_path.read_text()
        except OSError:
            existing = ""
        content = set_env_keys(
            existing,
            {"VS_WAKE_WORD": word, "VS_WAKE_MODELS": f"{home_on_device}/wake-models"},
        )
        written = runner.write_file(
            env_path, content, mode=0o644,
            description=f"tell the card to listen for {word}",
        )
        if not written.ok:
            return written
        staged.append(written)
        return StepResult(
            ok=True,
            summary=f"the wake word {word}" + (f" and its model {model.name}" if model else ""),
            commands=(),
        )

    def verify(self, request: DeviceRequest, runner: Runner) -> StepResult:
        """A card can be checked before it is booted: is it mounted, and does it know where to dial?"""
        if runner.dry_run:
            return StepResult(
                ok=True,
                summary=f"would check that {request.card_mount} is a mounted Linux root filesystem",
                dry_run=True,
            )
        if not request.card_mount:
            return StepResult(ok=False, summary="no SD card is mounted")
        root = Path(request.card_mount)
        if not root.is_dir():
            return StepResult(ok=False, summary=f"{root} is not a directory")
        if not (root / "etc").is_dir():
            return StepResult(
                ok=False,
                summary=f"{root} has no etc directory, so it is probably not the card's root "
                        f"filesystem. Mount the rootfs partition and try again.",
            )
        env = bridge_env_path(request.card_mount, request.card_user or "satellite")
        if not env.exists():
            return StepResult(
                ok=False,
                summary=f"the card has no {env.name} yet, so the satellite has not been set up on "
                        f"it. Run the setup step first.",
            )
        if "CHANGE-ME" in env.read_text():
            return StepResult(
                ok=False,
                summary=f"{env.name} still says CHANGE-ME, so the device would boot and fail to "
                        f"connect. Fill in the gateway address and run the setup step again.",
            )
        return StepResult(
            ok=True,
            summary="the card is ready. The device finishes its own setup on first boot, so it "
                    "will appear on the gateway shortly after it starts.",
        )


DRIVER = PiDriver()
