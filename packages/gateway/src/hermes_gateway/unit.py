"""A systemd user unit for the pairing endpoint.

The endpoint is useless if nobody starts it, and telling a person to type a long command at a shell
they may not have is not an install. This renders a unit from the same configuration the endpoint
itself uses, so the two cannot disagree about the state directory, the dial address or the port.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

UNIT_NAME = "hermes-pairing.service"

TEMPLATE = """\
[Unit]
Description=Hermes voice pairing endpoint
Documentation=https://github.com/omgitsgela/hermes-voice-satellite
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# A separate listener from the device registry: devices dial the registry, workbenches pair here.
ExecStart={python}{mode} --state {state} --dial {dial} --bind {bind} --port {port}
Restart=always
RestartSec=2
# The words are read from the state directory, and the certificate authority lives beside them, so
# this process needs to be the same user that installed the gateway.
WorkingDirectory={working_directory}

[Install]
WantedBy=default.target
"""


@dataclass(frozen=True)
class UnitConfig:
    python: Path
    state_dir: Path
    dial_address: str
    working_directory: Path
    bind: str = "0.0.0.0"
    port: int = 10704
    #: True when `python` is the packaged installer rather than an interpreter. The packaged
    #: executable is not a Python: `-m` would be handed to its own argument parser and it would exit
    #: instead of serving, so a frozen build asks it for the pairing server by name.
    frozen: bool = False


def render_unit(config: UnitConfig) -> str:
    """The unit file text. Paths are quoted for systemd, which splits ExecStart on whitespace."""
    def quote(path: object) -> str:
        text = str(path)
        return f'"{text}"' if " " in text else text

    if not config.dial_address:
        raise ValueError("a dial address is required: it is what a workbench connects to")
    mode = " --pairing-server" if config.frozen else " -m hermes_gateway.pairing_server"
    return TEMPLATE.format(
        python=quote(config.python),
        mode=mode,
        state=quote(config.state_dir),
        dial=quote(config.dial_address),
        bind=config.bind,
        port=config.port,
        working_directory=quote(config.working_directory),
    )


def write_unit(config: UnitConfig, target: Path) -> Path:
    """Write the unit where a user unit belongs, and return its path."""
    text = render_unit(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def enable_instructions(target: Path) -> tuple[str, ...]:
    """What a person (or the installer) has to run next. Printed, never run silently."""
    return (
        f"systemctl --user daemon-reload",
        f"systemctl --user enable --now {target.name}",
        f"systemctl --user status {target.name}",
    )
