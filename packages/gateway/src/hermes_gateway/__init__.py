"""The gateway side of the installer: what it produces for a person to keep, and what it runs.

Two artefacts matter here. The **identity code**, derived from the gateway's certificate, and the
**pairing words**. Both are generated once, shown once, and explained, because the person will be
asked for them later and there is no way to recover them politely.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

IDENTITY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

INSTALL_SCRIPT = "scripts/install-gateway.sh"

#: What the installer found on this machine. Desktop and a service install differ in who supervises
#: the bridge, so the UI says which one it is rather than assuming.
HERMES_SHAPES = ("desktop", "service", "none")


def _encode(raw: bytes) -> str:
    bits = "".join(f"{byte:08b}" for byte in raw)
    out = []
    for start in range(0, len(bits) - len(bits) % 5, 5):
        out.append(IDENTITY_ALPHABET[int(bits[start:start + 5], 2)])
    return "".join(out)


def identity_code_from_der(der: bytes) -> str:
    """The same code the installer core derives, so both sides agree without sharing code."""
    if not der:
        raise ValueError("no certificate to derive an identity code from")
    bare = _encode(hashlib.sha256(der).digest()[:5])[:8]
    return f"{bare[:4]}-{bare[4:]}"


def load_certificate(path: Path) -> bytes:
    """Accept DER or PEM; return DER, because the fingerprint is over the DER form."""
    raw = path.read_bytes()
    if b"-----BEGIN CERTIFICATE-----" not in raw:
        return raw
    text = raw.decode("ascii", "replace")
    body = "".join(
        line.strip()
        for line in text.splitlines()
        if line and not line.startswith("-----")
    )
    import base64

    return base64.b64decode(body)


@dataclass(frozen=True)
class PairingCard:
    """What the person is told to write down, and what the config tool later asks them to match."""

    identity_code: str
    words: str
    dial_address: str

    def lines(self) -> tuple[str, ...]:
        return (
            f"Gateway address : {self.dial_address}",
            f"Identity code   : {self.identity_code}",
            f"Pairing words   : {self.words}",
        )

    def as_text(self) -> str:
        return "\n".join(self.lines())


def pairing_card(certificate_path: Path, words: str, dial_address: str) -> PairingCard:
    if not words.strip():
        raise ValueError("the pairing words are empty; nothing to show")
    return PairingCard(
        identity_code=identity_code_from_der(load_certificate(certificate_path)),
        words=words.strip(),
        dial_address=dial_address,
    )


# -- what Hermes is on this machine -------------------------------------------------------------


def detect_hermes(
    which: Callable[[str], str | None] = shutil.which,
    path_exists: Callable[[Path], bool] = Path.exists,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> str:
    """Return one of `HERMES_SHAPES`.

    `desktop` is a Hermes desktop application with its own runtime; `service` is a Hermes install
    whose gateway is supervised on the host. The installer adapts supervision to whichever it finds,
    which is why it asks rather than assuming.
    """
    environment = dict(env or {})
    base_home = home or Path.home()

    if environment.get("HERMES_DESKTOP") or environment.get("HERMES_DESKTOP_APP"):
        return "desktop"

    cli = which("hermes")
    if cli:
        try:
            listed = subprocess.run(
                [cli, "--help"], capture_output=True, text=True, timeout=30
            ).stdout
        except (OSError, subprocess.SubprocessError):
            listed = ""
        if "desktop (gui)" in listed:
            return "desktop"
        return "service"

    if path_exists(base_home / ".hermes" / "config.yaml"):
        return "service"
    return "none"


def install_commands(
    *,
    base: str,
    device_reachable_as: str,
    registry_port: int = 10701,
    tls_port: int = 10702,
    skip_hermes: bool = False,
) -> tuple[tuple[str, ...], ...]:
    """The gateway install, as argv. The script is the engine; this decides what to ask it for."""
    if not device_reachable_as:
        raise ValueError("--device-reachable-as is required: it must be the address devices dial")
    if device_reachable_as.startswith(("127.", "0.0.0.0", "localhost")):
        raise ValueError(
            f"{device_reachable_as} cannot be reached by a device, so it cannot be the dial address"
        )
    argv = [
        INSTALL_SCRIPT,
        "--base", base,
        "--device-reachable-as", device_reachable_as,
        "--registry-port", str(registry_port),
        "--tls-port", str(tls_port),
    ]
    if skip_hermes:
        argv.append("--skip-hermes")
    return (tuple(argv),)


def run_commands(
    commands: Sequence[Sequence[str]],
    *,
    dry_run: bool,
    executor: Callable[[Sequence[str]], tuple[int, str]] | None = None,
) -> tuple[bool, str]:
    if dry_run:
        return True, "\n".join(" ".join(c) for c in commands)
    outputs = []
    for command in commands:
        if executor is not None:
            code, output = executor(command)
        else:
            try:
                finished = subprocess.run(list(command), capture_output=True, text=True)
            except OSError as exc:
                # A missing script, or one that Windows cannot execute, is an outcome to report.
                return False, f"could not run {command[0]}: {exc}"
            code, output = finished.returncode, (finished.stdout or "") + (finished.stderr or "")
        outputs.append(output)
        if code != 0:
            return False, "\n".join(outputs)
    return True, "\n".join(outputs)
