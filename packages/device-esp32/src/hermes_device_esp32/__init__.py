"""The ESP32 XVF3800 satellite: a profile, and a driver that builds commands.

Flags below are the ones `scripts/install-esp32.sh` actually parses: `--port`, `--flash`,
`--provision`, `--device-id`, `--device-name`, `--endpoint`, `--tls-certs`, `--model`,
`--model-name`, `--wifi-ssid`, `--wifi-psk-file`.

Two details the first draft got wrong, both found by reading the script rather than by testing it:

- the port flag is `--port`, not `--device`;
- `--port` alone does nothing. The script needs an explicit `--flash` or `--provision` action, and
  provisioning refuses to run without `--device-id`, which is the board's own identity and not the
  friendly name a person chooses.

The wake word is not a flag of its own: it is the model's detection name, so a chosen wake word
travels as `--model` plus `--model-name`.
"""
from __future__ import annotations

from hermes_installer.driver import (
    Command,
    DeviceRequest,
    Runner,
    StepResult,
    require_posix,
)
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
    coprocessor_flash=True,
    description=(
        "A small box with a four-microphone array and its own speaker. The audio processing runs on "
        "the array itself, so the board only carries the stream."
    ),
    identify=(
        "Look for XIAO ESP32S3 printed on the board next to the USB socket, with a round "
        "four-microphone array and a small speaker underneath."
    ),
    settings={
        "voice": Setting("voice", "next_turn", remote=True,
                         help="Synthesized on the Satellite Gateway; independent for each device."),
        "tls_credentials": Setting("tls_credentials", "cable", remote=False),
        "wake_model": Setting("wake_model", "cable", remote=False),
        "playback_volume": Setting("playback_volume", "immediate", remote=True,
                                   help="How loud it speaks."),
        "mic_volume": Setting("mic_volume", "immediate", remote=True,
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

# The models that ship in the checkout, by selector. A word in this tuple can be
# provisioned without the person supplying a file; anything else needs its own
# model, because the firmware selects a model by name and a name with no model
# leaves the satellite deaf.
BUNDLED_MODELS = ("hey_hermes", "okay_nabu")


def _bundled_model(word: str) -> str | None:
    return f"firmware/esp32/models/{word}.tflite" if word in BUNDLED_MODELS else None


def _resolved_model(request: DeviceRequest) -> str | None:
    return request.wake_model or _bundled_model(request.wake_word) or None


def _model_problem(request: DeviceRequest) -> str | None:
    """Why this request cannot be provisioned as asked, or None.

    A wake word is a selector: the firmware loads a model only when the blob's
    name matches it. Provisioning the default model while selecting another
    word is the failure this refuses, because the result is a satellite that is
    configured, reports healthy, and never wakes.
    """
    if request.wake_word and not _resolved_model(request):
        return (f"the wake word {request.wake_word!r} has no model in this checkout; "
                f"choose one of {', '.join(BUNDLED_MODELS)} or supply the model file")
    return None


def _argv(request: DeviceRequest, action: str) -> tuple[str, ...]:
    argv = [INSTALL_SCRIPT, "--port", request.port or "", action, "--endpoint",
            request.dial_address]
    if request.device_id:
        argv += ["--device-id", request.device_id]
    if request.device_name:
        argv += ["--device-name", request.device_name]
    if request.certs_dir:
        argv += ["--tls-certs", request.certs_dir]
    if request.registry_file:
        # Pairing issued this record for this device; without it the provisioner refuses to write,
        # because it cannot confirm the certificate is the one the gateway registered.
        argv += ["--registry", request.registry_file]
    model = _resolved_model(request)
    if model:
        # Passing the model explicitly is what keeps the phrase the installer showed and the phrase
        # the device answers to the same phrase: without --model the script falls back to its own
        # default, which is a different model with a different selector.
        argv += ["--model", model, "--model-name", request.wake_word]
    if request.wifi_ssid and request.wifi_psk_file:
        argv += ["--wifi-ssid", request.wifi_ssid, "--wifi-psk-file", request.wifi_psk_file]
    return tuple(argv)


class Esp32Xvf3800Driver:
    PROFILE = PROFILE

    def flash(self, request: DeviceRequest, runner: Runner) -> StepResult:
        if not request.port:
            return StepResult(ok=False, summary="no USB port selected for this device")
        if not request.dial_address:
            return StepResult(ok=False, summary="no gateway address has been chosen yet")
        problem = _model_problem(request)
        if problem:
            return StepResult(ok=False, summary=problem)
        require_posix("Flashing the board")
        return runner.run(
            Command(
                _argv(request, "--flash"),
                f"write the firmware and point the device at {request.dial_address}",
                secret_file=request.wifi_psk_file,
            )
        )

    def coprocessor(self, request: DeviceRequest, runner: Runner) -> StepResult:
        """Write the XVF3800 audio chip over DFU: a SEPARATE operation from flash/provision.

        The chip has its own USB-C socket (next to the audio jack) and only appears while the
        board is in Safe Mode (hold Mute while re-applying power until the red LED blinks), so
        the installer runs it as its own step with its own cabling, never alongside the ESP32
        write. A first-time board needs it: the current firmware runs the I2S link at 48 kHz
        with the chip as clock master, and the stock image leaves the microphone path dead
        (firmware/stock/README.md, "Co-processor coupling").
        """
        require_posix("Writing the audio chip")
        argv = [INSTALL_SCRIPT, "--coprocessor"]
        if (request.extra or {}).get("coprocessor_factory"):
            argv.append("--restore-factory")
        return runner.run(Command(
            tuple(argv),
            "write the audio chip (separate operation: its own USB socket, Safe Mode, "
            "hash-verified image)",
        ))

    def provision(self, request: DeviceRequest, runner: Runner) -> StepResult:
        """A later run: change the staged settings without reflashing the application."""
        if not request.port:
            return StepResult(ok=False, summary="no USB port selected for this device")
        if not request.device_id:
            return StepResult(
                ok=False,
                summary="the board's own identity is needed to stage settings, and it has not "
                        "been read yet. Read it from the device first.",
            )
        problem = _model_problem(request)
        if problem:
            return StepResult(ok=False, summary=problem)
        if request.extra.get("manage_only"):
            # First-time provisioning deliberately requires a complete credential set. A later
            # settings edit must not enter that script: it would demand pairing again and replace
            # unrelated settings. Use the existing console verbs for only the requested changes.
            from hermes_installer.driver import merge

            commands = []
            base = (PROVISION_SCRIPT, "--port", request.port)
            if request.wifi_ssid:
                if not request.wifi_psk_file and not runner.dry_run:
                    return StepResult(ok=False, summary="a Wi-Fi password is needed for this network")
                commands.append(Command(
                    (*base, "save-profile", request.wifi_ssid,
                     request.wifi_psk_file or "<temporary-password-file>"),
                    "save the selected Wi-Fi network", secret_file=request.wifi_psk_file,
                ))
            if request.wake_word or request.wake_model:
                model = _resolved_model(request)
                if model:
                    commands.append(Command(
                        ("scripts/provision-device-model.py", "--port", request.port,
                         "--model", model, "--name", request.wake_word),
                        "write the selected wake model (not firmware)",
                    ))
                commands.append(Command((*base, "set-wake", request.wake_word),
                                        "select the wake word"))
            results = []
            for command in commands:
                result = runner.run(command)
                results.append(result)
                if not result.ok:
                    return merge(*results)
            return merge(*results, summary="Settings saved. Power-cycle the device to use them.")
        return runner.run(
            Command(
                _argv(request, "--provision"),
                "stage the gateway address, Wi-Fi and model on the device",
                secret_file=request.wifi_psk_file,
            )
        )

    def rom_identity(self, port: str, runner: Runner) -> StepResult:
        """The identity of a board that cannot answer the console yet.

        A new board, or one still carrying a vendor image, has no satellite firmware to ask. Without
        this the installer advertised as writing the firmware could only prepare a board somebody had
        already flashed by another route. The ROM bootloader always answers, and the identity is
        derived from the same factory MAC the firmware uses.
        """
        if not port:
            return StepResult(ok=False, summary="no USB port selected for this device")
        return runner.run(
            Command(
                (PROVISION_SCRIPT, "--port", port, "rom-mac"),
                "read the board's factory identity from its ROM bootloader",
            )
        )

    def verify(self, request: DeviceRequest, runner: Runner) -> StepResult:
        if not request.port:
            return StepResult(ok=False, summary="no USB port selected for this device")
        return runner.run(
            Command(
                (PROVISION_SCRIPT, "--port", request.port, "device-id"),
                "read the board's own identity",
            )
        )


DRIVER = Esp32Xvf3800Driver()
