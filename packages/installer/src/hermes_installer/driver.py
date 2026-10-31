"""Drivers: three verbs, and commands that can be inspected instead of executed.

A driver never touches a subprocess directly. It builds `Command` objects, and the runner decides
whether to run them. That is what makes the whole install testable on a machine with no device
attached: the test asserts the exact argv a driver would use, and nothing is flashed.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence


@dataclass(frozen=True)
class Command:
    """One step: what it is for, and the argv that carries it out."""

    argv: tuple[str, ...]
    description: str
    #: Writing a disk or installing a unit needs elevation; the UI asks once, visibly.
    requires_elevation: bool = False
    #: A file holding a secret (a passphrase). Its contents are never read into a log or an error.
    secret_file: str | None = None

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError(f"{self.description}: a command needs an argv")

    def redacted(self) -> tuple[str, ...]:
        """The argv safe to display: the secret file's path is shown, never its contents."""
        return self.argv


@dataclass(frozen=True)
class StepResult:
    ok: bool
    summary: str
    commands: tuple[Command, ...] = ()
    output: str = ""
    dry_run: bool = False


class Runner:
    """Executes commands, or records them when the caller is only previewing."""

    def __init__(
        self,
        dry_run: bool = False,
        executor: Callable[[Sequence[str]], tuple[int, str]] | None = None,
    ) -> None:
        self.dry_run = dry_run
        self._executor = executor or _subprocess_executor

    def run(self, command: Command) -> StepResult:
        if self.dry_run:
            return StepResult(
                ok=True,
                summary=f"would: {command.description}",
                commands=(command,),
                output=" ".join(command.redacted()),
                dry_run=True,
            )
        code, output = self._executor(command.argv)
        return StepResult(
            ok=code == 0,
            summary=command.description if code == 0 else f"{command.description} failed",
            commands=(command,),
            output=output,
        )

    def run_all(self, commands: Sequence[Command]) -> StepResult:
        collected: list[Command] = []
        for command in commands:
            result = self.run(command)
            collected.append(command)
            if not result.ok:
                return StepResult(
                    ok=False,
                    summary=result.summary,
                    commands=tuple(collected),
                    output=result.output,
                    dry_run=self.dry_run,
                )
        return StepResult(
            ok=True,
            summary=f"{len(collected)} step(s) complete",
            commands=tuple(collected),
            output="" if self.dry_run else "\n".join([""]),
            dry_run=self.dry_run,
        )


def _subprocess_executor(argv: Sequence[str]) -> tuple[int, str]:
    try:
        finished = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=900
        )
    except FileNotFoundError as exc:
        return 127, str(exc)
    except subprocess.TimeoutExpired:
        return 124, f"timed out: {' '.join(argv)}"
    return finished.returncode, (finished.stdout or "") + (finished.stderr or "")


@dataclass
class DeviceRequest:
    """What the flow collected on screen, handed to a driver unchanged."""

    device_name: str
    dial_address: str
    wake_word: str = "hey_hermes"
    voice: str = "en_US-hfc_female-medium"
    port: str | None = None            # usb_serial devices
    card_mount: str | None = None      # sd_card devices
    wifi_ssid: str | None = None
    wifi_psk_file: str | None = None
    certs_dir: str | None = None
    #: A wake-word model file for devices that take one (a microWakeWord .tflite, for instance).
    wake_model: str | None = None
    extra: dict = field(default_factory=dict)


class DeviceDriver(Protocol):
    """What a device package must provide. Three verbs, no UI, no policy."""

    PROFILE: object

    def flash(self, request: DeviceRequest, runner: Runner) -> StepResult: ...

    def provision(self, request: DeviceRequest, runner: Runner) -> StepResult: ...

    def verify(self, request: DeviceRequest, runner: Runner) -> StepResult: ...
