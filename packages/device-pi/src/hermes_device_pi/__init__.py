"""The Raspberry Pi satellite: a profile, and a driver that builds commands.

This device is set up by writing its SD card, not by a cable into the Pi. The first draft of this
driver was wrong in a way worth recording, because the test passed: it called
`scripts/install-pi.sh --base <card mount>`, but `--base` is the *software directory on the device*,
and that script installs systemd units into `/etc/systemd/system` and calls the host's `systemctl`.
Pointed at a mounted card it would have failed without root, or worse, installed satellite services
on the workbench.

The right script is `scripts/deploy-to-card.sh <mount> [user]`, which copies the device files onto
the card and writes a placeholder `bridge-link.env`. That script deliberately never overwrites the
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
    return Path(mount) / "home" / user / "bridge-link.env"


def render_bridge_env(existing: str, *, device_id: str, host: str, port: int, name: str) -> str:
    """Set the four values the card needs, keeping every other line the template put there."""
    wanted = {
        "VOICE_DEVICE_ID": device_id,
        "VOICE_BRIDGE_HOST": host,
        "VOICE_BRIDGE_PORT": str(port),
        "VOICE_DEVICE_NAME": name,
    }
    lines = existing.splitlines() if existing else [
        "# Bootstrap configuration for the outbound shape: the device dials the bridge.",
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
        """Copy a saved Wi-Fi profile onto the card. Needs root: the source profile is root-readable.

        Note the shape: this copies a network this machine already knows. It cannot invent one from
        a passphrase, so the interface must not offer to.
        """
        if not request.card_mount:
            return StepResult(ok=False, summary="no SD card is mounted")
        if not request.wifi_ssid:
            return StepResult(ok=False, summary="no saved Wi-Fi network was chosen")
        require_posix("Copying a Wi-Fi profile onto a card")
        return runner.run(
            Command(
                (WIFI_SCRIPT, "--mount", request.card_mount, "--source", request.wifi_ssid,
                 "--name", request.wifi_ssid),
                f"copy the saved Wi-Fi network {request.wifi_ssid} onto the card",
                requires_elevation=True,
            )
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
