"""The ESP32 XVF3800 satellite: a profile, and a driver that builds commands.

Flags below are the ones `scripts/install-esp32.sh` actually accepts: `--device`, `--endpoint`,
`--tls-certs`, `--model`, `--model-name`, `--wifi-ssid`, `--wifi-psk-file`, `--provision`. The wake
word is not a flag of its own: it is the model's detection name, so a chosen wake word travels as
`--model` plus `--model-name`.

Everything here is data plus argv. No subprocess starts unless the runner is told to run, which is
what lets the whole flow be tested with no board attached.
"""
from __future__ import annotations

from hermes_installer.driver import Command, DeviceRequest, Runner, StepResult
from hermes_installer.profile import DeviceProfile, Setting

PROFILE = DeviceProfile(
    id="esp32-xvf3800",
    display_name="ESP32-S3 with reSpeaker XVF3800",
    vendor="Seeed",
    flashing="usb_serial",
    wake_formats=("microwakeword_tflite",),
    voice="local_piper",
    wake_default="hey_hermes",
    first_party=True,
    description=(
        "A USB-C satellite with a four-mic array and its own speaker. The audio processing runs on "
        "the array's co-processor, so the ESP32 only carries the stream."
    ),
    settings={
        "playback_volume": Setting("playback_volume", "immediate", remote=True,
                                   help="How loud it speaks."),
        "mic_gain": Setting("mic_gain", "immediate", remote=True,
                            help="How sensitive the microphones are. 78 is unity; higher makes a "
                                 "quiet room look like speech."),
        "led_brightness": Setting("led_brightness", "service_restart", remote=True),
        "wake_threshold": Setting("wake_threshold", "service_restart", remote=True,
                                  help="A second gate on the detector's own score, so it can only "
                                       "make the wake word harder to trigger."),
        "wake_word": Setting("wake_word", "cable", remote=False,
                             help="A different wake word means a different model in flash, so it "
                                  "needs the cable."),
        "wifi": Setting("wifi", "cable", remote=False,
                        help="Wi-Fi is staged over the cable; there is no remote path yet."),
        "dial_address": Setting("dial_address", "cable", remote=False,
                                help="Where the device connects. Cable only."),
    },
)

INSTALL_SCRIPT = "scripts/install-esp32.sh"
PROVISION_SCRIPT = "scripts/esp32_provision.py"


def _base_argv(request: DeviceRequest) -> list[str]:
    argv = [
        INSTALL_SCRIPT,
        "--device", request.port or "",
        "--endpoint", request.dial_address,
    ]
    if request.certs_dir:
        argv += ["--tls-certs", request.certs_dir]
    if request.wake_model:
        argv += ["--model", request.wake_model, "--model-name", request.wake_word]
    if request.wifi_ssid and request.wifi_psk_file:
        argv += ["--wifi-ssid", request.wifi_ssid, "--wifi-psk-file", request.wifi_psk_file]
    return argv


class Esp32Xvf3800Driver:
    PROFILE = PROFILE

    def flash(self, request: DeviceRequest, runner: Runner) -> StepResult:
        if not request.port:
            return StepResult(ok=False, summary="no USB port selected for this device")
        if not request.dial_address:
            return StepResult(ok=False, summary="no gateway address has been chosen yet")
        command = Command(
            tuple(_base_argv(request)),
            f"write the firmware and point the device at {request.dial_address}",
            secret_file=request.wifi_psk_file,
        )
        return runner.run(command)

    def provision(self, request: DeviceRequest, runner: Runner) -> StepResult:
        """A later run: change the staged settings without reflashing the application."""
        if not request.port:
            return StepResult(ok=False, summary="no USB port selected for this device")
        argv = tuple(_base_argv(request) + ["--provision"])
        return runner.run(
            Command(argv, "stage the gateway address, Wi-Fi and model on the device",
                    secret_file=request.wifi_psk_file)
        )

    def verify(self, request: DeviceRequest, runner: Runner) -> StepResult:
        if not request.port:
            return StepResult(ok=False, summary="no USB port selected for this device")
        return runner.run(
            Command(
                (PROVISION_SCRIPT, "--port", request.port, "status"),
                "read the device's own report",
            )
        )


DRIVER = Esp32Xvf3800Driver()
