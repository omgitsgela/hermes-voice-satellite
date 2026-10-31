"""The Raspberry Pi satellite: a profile, and a driver that builds commands.

This device is flashed by writing its SD card, not by plugging a cable into it. The card goes into a
reader, the installer writes to the mounted filesystem, and the Wi-Fi profile is copied onto the card
so the passphrase never has to be typed into a form, echoed into a log, or pasted into a chat.

Flags are the ones `scripts/install-pi.sh` accepts (`--base`, `--device-name`, `--wake-word`,
`--endpoint`, `--tls-certs`, `--user`) and the ones `scripts/add-wifi-profile-to-card.py` accepts
(`--mount`, `--source`, `--name`, `--priority`).
"""
from __future__ import annotations

from pathlib import Path

from hermes_installer.driver import Command, DeviceRequest, Runner, StepResult
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
        "A small Linux satellite. Its wake word runs as openWakeWord on the Pi itself, so the model "
        "format is different from the ESP32's even when the phrase is the same."
    ),
    settings={
        "playback_volume": Setting("playback_volume", "immediate", remote=True),
        "mic_gain": Setting("mic_gain", "immediate", remote=True),
        "led_brightness": Setting("led_brightness", "service_restart", remote=True),
        "wake_threshold": Setting("wake_threshold", "service_restart", remote=True),
        "wake_word": Setting("wake_word", "service_restart", remote=True,
                             help="On this device the model lives on the card, so once it is there "
                                  "the wake word can be switched from the gateway."),
        "wifi": Setting("wifi", "cable", remote=False,
                        help="Wi-Fi lives on the card. Changing it means the card."),
        "dial_address": Setting("dial_address", "cable", remote=False),
    },
)

INSTALL_SCRIPT = "scripts/install-pi.sh"
WIFI_SCRIPT = "scripts/add-wifi-profile-to-card.py"


class PiDriver:
    PROFILE = PROFILE

    def flash(self, request: DeviceRequest, runner: Runner) -> StepResult:
        if not request.card_mount:
            return StepResult(ok=False, summary="no SD card is mounted")
        if not request.dial_address:
            return StepResult(ok=False, summary="no gateway address has been chosen yet")
        argv = [
            INSTALL_SCRIPT,
            "--base", request.card_mount,
            "--device-name", request.device_name,
            "--wake-word", request.wake_word,
            "--endpoint", request.dial_address,
        ]
        if request.certs_dir:
            argv += ["--tls-certs", request.certs_dir]
        if request.extra.get("user"):
            argv += ["--user", str(request.extra["user"])]
        return runner.run(
            Command(tuple(argv), f"install the satellite onto the card and point it at "
                                 f"{request.dial_address}")
        )

    def provision(self, request: DeviceRequest, runner: Runner) -> StepResult:
        """Copy a saved Wi-Fi profile onto the card. Needs root: the profile is root-readable only."""
        if not request.card_mount:
            return StepResult(ok=False, summary="no SD card is mounted")
        if not request.wifi_ssid:
            return StepResult(ok=False, summary="no Wi-Fi network was chosen")
        argv = [
            WIFI_SCRIPT,
            "--mount", request.card_mount,
            "--source", request.wifi_ssid,
            "--name", request.wifi_ssid,
        ]
        return runner.run(
            Command(tuple(argv),
                    f"copy the saved Wi-Fi profile for {request.wifi_ssid} onto the card",
                    requires_elevation=True)
        )

    def verify(self, request: DeviceRequest, runner: Runner) -> StepResult:
        """A card can be checked before it is booted: is it mounted, and does it look like Linux?"""
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
        return StepResult(
            ok=True,
            summary="the card is mounted and looks like a Linux root filesystem. The device "
                    "finishes its own setup on first boot, so it will appear on the gateway next.",
        )


DRIVER = PiDriver()
