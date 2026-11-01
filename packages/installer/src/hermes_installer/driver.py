"""Drivers: three verbs, and commands that can be inspected instead of executed.

A driver never touches a subprocess directly. It builds `Command` objects, and the runner decides
whether to run them, from which directory, and how to report a failure. That is what makes the whole
install testable on a machine with no device attached.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
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


def merge(*results: "StepResult", summary: str = "") -> "StepResult":
    """Combine steps into one outcome, stopping short of claiming success if any stage failed."""
    commands: tuple[Command, ...] = tuple(c for r in results for c in r.commands)
    ok = all(r.ok for r in results)
    failed = next((r for r in results if not r.ok), None)
    return StepResult(
        ok=ok,
        summary=summary or (failed.summary if failed else "; ".join(r.summary for r in results)),
        commands=commands,
        output="\n".join(r.output for r in results if r.output),
        dry_run=all(r.dry_run for r in results),
    )


class UnsupportedOnThisPlatform(Exception):
    """An operation this OS genuinely cannot perform, said plainly instead of half-run."""


class Runner:
    """Executes commands, or records them when the caller is only previewing.

    `repo_root` exists because drivers name the repository's own scripts by relative path. A CLI run
    from anywhere else would otherwise look for `scripts/install-esp32.sh` in the wrong place, so a
    relative script path is resolved against the checkout the runner was given and executed there.
    """

    def __init__(
        self,
        dry_run: bool = False,
        executor: Callable[[Sequence[str], Path | None], tuple[int, str]] | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.dry_run = dry_run
        self._executor = executor or _subprocess_executor
        self.repo_root = Path(repo_root) if repo_root else None

    def resolve(self, argv: Sequence[str]) -> tuple[str, ...]:
        """Turn a repository-relative script into an absolute one, so cwd cannot hide it."""
        if not argv:
            return ()
        first = str(argv[0])
        candidate = Path(first)
        if not candidate.is_absolute() and self.repo_root is not None:
            candidate = self.repo_root / first
        resolved = str(candidate) if candidate.exists() else first
        rest = tuple(str(a) for a in argv[1:])
        if Path(resolved).suffix == ".py":
            # Helpers are source files (mode 0644), not executable programs. The frozen installer
            # is the Python runtime on a clean machine; a shebang would silently require python3.
            if getattr(sys, "frozen", False):
                return (sys.executable, "--run-python", resolved, *rest)
            python = sys.executable
            if self.repo_root is not None:
                venv = self.repo_root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32"
                                                    else "bin/python")
                if venv.is_file():
                    python = str(venv)
            return (python, resolved, *rest)
        return (resolved, *rest)

    def run(self, command: Command) -> StepResult:
        argv = self.resolve(command.argv)
        if command.requires_elevation and not self.dry_run and not _is_elevated():
            return StepResult(
                ok=False,
                summary=("this step needs administrator rights. Run the installer with sudo, or "
                         "perform this step where you can."),
            )
        if self.dry_run:
            return StepResult(
                ok=True,
                summary=f"would: {command.description}",
                commands=(command,),
                output=" ".join(argv),
                dry_run=True,
            )
        code, output = self._executor(argv, self.repo_root)
        return StepResult(
            ok=code == 0,
            summary=command.description if code == 0 else f"{command.description} failed",
            commands=(command,),
            output=output,
        )

    def write_file(
        self, path: Path, content: str, *, mode: int = 0o600, description: str = ""
    ) -> StepResult:
        """Write a file the install needs, respecting dry-run. Used for a card's env file.

        This is the one place a driver touches the filesystem, and it exists because the SD-card
        script deliberately refuses to overwrite a file holding real values, so the values have to
        come from here.
        """
        if self.dry_run:
            return StepResult(
                ok=True, summary=f"would: {description}", output=str(path), dry_run=True
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            os.chmod(path, mode)
        except OSError as exc:
            return StepResult(ok=False, summary=f"could not write {path}: {exc}")
        return StepResult(ok=True, summary=description, output=str(path))

    def copy_file(
        self, source: Path, destination: Path, *, mode: int = 0o644, description: str = ""
    ) -> StepResult:
        """Copy a file the install needs (a wake-word model), respecting dry-run.

        ``write_file`` is text-only, and a model is a binary blob: decoding it as text would corrupt
        it while still reporting success.
        """
        if self.dry_run:
            return StepResult(
                ok=True, summary=f"would: {description}", output=str(destination), dry_run=True
            )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            os.chmod(destination, mode)
        except OSError as exc:
            return StepResult(ok=False, summary=f"could not copy to {destination}: {exc}")
        return StepResult(ok=True, summary=description, output=str(destination))

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
            dry_run=self.dry_run,
        )


def _subprocess_executor(argv: Sequence[str], cwd: Path | None) -> tuple[int, str]:
    from .environment import merge

    try:
        finished = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=1800, cwd=str(cwd) if cwd else None,
            # In a packaged build this carries HERMES_INSTALLER_PY, so the scripts run their Python
            # helpers on the bundled runtime instead of a python3 the machine may not have.
            env=merge(),
        )
    except FileNotFoundError as exc:
        return 127, f"could not run {argv[0]}: {exc}"
    except PermissionError as exc:
        # On Windows this is what a POSIX shell script produces: present, but not runnable.
        return 126, f"could not execute {argv[0]}: {exc}"
    except OSError as exc:
        return 126, f"could not execute {argv[0]}: {exc}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out: {' '.join(argv)}"
    return finished.returncode, (finished.stdout or "") + (finished.stderr or "")


def _is_elevated() -> bool:
    """Whether this process can do the things that need an administrator."""
    return os.geteuid() == 0 if hasattr(os, "geteuid") else False


def require_posix(operation: str) -> None:
    """The device work is shell and systemd. Say so, rather than failing in a confusing way."""
    if sys.platform == "win32":
        raise UnsupportedOnThisPlatform(
            f"{operation} runs the repository's shell scripts, which need Linux or macOS. On "
            f"Windows, do this part on the machine that holds the device, or inside WSL."
        )


@dataclass
class DeviceRequest:
    """What the flow collected on screen, handed to a driver unchanged."""

    device_name: str
    dial_address: str
    wake_word: str = "hey_hermes"
    voice: str = "en_US-hfc_female-medium"
    #: The board's own identity. Not the friendly name a person chose, and some scripts require it.
    device_id: str | None = None
    port: str | None = None            # usb_serial devices
    card_mount: str | None = None      # sd_card devices
    card_user: str = "satellite"
    wifi_ssid: str | None = None
    wifi_psk_file: str | None = None
    certs_dir: str | None = None
    #: The one-line registry record pairing wrote for this device, for provisioners that check it.
    registry_file: str | None = None
    #: A wake-word model file for devices that take one (a microWakeWord .tflite, for instance).
    wake_model: str | None = None
    extra: dict = field(default_factory=dict)


class DeviceDriver(Protocol):
    """What a device package must provide. Three verbs, no UI, no policy."""

    PROFILE: object

    def flash(self, request: DeviceRequest, runner: Runner) -> StepResult: ...

    def provision(self, request: DeviceRequest, runner: Runner) -> StepResult: ...

    def verify(self, request: DeviceRequest, runner: Runner) -> StepResult: ...
