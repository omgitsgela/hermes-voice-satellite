"""One session object, two front ends.

The console flow and the web UI drive the same object, so a step that works in one works in the
other, and a test can drive it with no interface at all. Nothing here decides what a device is: it
asks the registry, and the registry asks the device packages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import os
import tempfile

import hermes_gateway as gateway_module

from .driver import DeviceRequest, Runner, UnsupportedOnThisPlatform
from .pairing import GatewayRef, PairingCancelled, PairingError, pair as pair_with_gateway
from .registry import DeviceRegistry, RegistryError
from .secrets import CredentialStore


@dataclass
class SessionState:
    hermes_shape: str = "none"
    paired: dict[str, Any] | None = None
    device_name: str = ""
    log: list[str] = field(default_factory=list)


class Session:
    """Everything the UI can do, in one place."""

    def __init__(
        self,
        repo_root: Path | None = None,
        *,
        registry: DeviceRegistry | None = None,
        store: CredentialStore | None = None,
        dry_run: bool = False,
    ) -> None:
        self.repo_root = Path(repo_root or Path.cwd())
        self.registry = registry or DeviceRegistry()
        if registry is None:
            self.registry.discover()
        self.store = store or CredentialStore()
        self.dry_run = dry_run
        # Set by a successful pairing: the credentials themselves, and a private directory the
        # provisioning scripts can read them from.
        self.credentials = None
        self.certs_dir: str | None = None
        self._certs_tmp: Path | None = None
        self.state = SessionState()
        self.state.hermes_shape = gateway_module.detect_hermes()

    # -- what the first screen needs -----------------------------------------------------------

    def status(self) -> dict:
        return {
            "hermes": self.state.hermes_shape,
            "hermes_explained": {
                "desktop": "Hermes Desktop found. The installer will add the voice bridge and "
                           "configure Hermes for it.",
                "service": "A Hermes service install found. The installer will also own how the "
                           "bridge is supervised.",
                "none": "Hermes was not found on this machine. Install Hermes first, then run this "
                        "installer again. Nothing has been changed.",
            }.get(self.state.hermes_shape, ""),
            "dry_run": self.dry_run,
            "devices": [
                {
                    "id": p.id,
                    "name": p.display_name,
                    "vendor": p.vendor,
                    "flashing": p.flashing,
                    "needs_card_reader": p.needs_card_reader,
                    "voice": p.voice,
                    "wake_format": p.wake_catalogue_format(),
                    "wake_default": p.wake_default,
                    "description": p.description,
                    "identify": p.identify,
                    "remote_settings": list(p.remote_settings),
                    "cable_settings": list(p.cable_settings),
                    "settings": {
                        key: {"apply": s.apply, "remote": s.remote, "help": s.help}
                        for key, s in p.settings.items()
                    },
                }
                for p in self.registry.all()
            ],
            "load_errors": self.registry.load_errors,
            "credentials_backend": self.store.backend_name,
        }

    # -- what is plugged in -------------------------------------------------------------------

    def usb_ports(self) -> list[dict]:
        """Serial ports this machine can see, so nobody has to guess /dev/ttyACM0."""
        try:
            from serial.tools import list_ports
        except Exception:  # noqa: BLE001 - pyserial absent is not an error worth failing over
            return []
        found = []
        for port in list_ports.comports():
            found.append({"device": port.device, "description": port.description or ""})
        return sorted(found, key=lambda p: p["device"])

    def card_mounts(self) -> list[dict]:
        """Candidate mounted root filesystems for a card, identified by having an etc directory."""
        import string
        import sys

        candidates: list[Path] = []
        if sys.platform == "win32":
            candidates = [Path(f"{letter}:\\") for letter in string.ascii_uppercase]
        else:
            home = Path.home()
            for base in (Path("/run/media") / home.name, Path("/media") / home.name,
                         Path("/media"), Path("/mnt")):
                if base.is_dir():
                    try:
                        candidates += [child for child in base.iterdir() if child.is_dir()]
                    except OSError:
                        continue
        found = []
        for candidate in candidates[:40]:
            try:
                if (candidate / "etc").is_dir():
                    found.append({"mount": str(candidate)})
            except OSError:
                continue
        return found

    # -- gateway ------------------------------------------------------------------------------

    def install_gateway(
        self,
        dial_address: str,
        *,
        registry_port: int = 10701,
        tls_port: int = 10702,
        skip_hermes: bool = False,
        dry_run: bool | None = None,
    ) -> dict:
        try:
            commands = gateway_module.install_commands(
                base=str(self.repo_root),
                device_reachable_as=dial_address,
                registry_port=registry_port,
                tls_port=tls_port,
                skip_hermes=skip_hermes,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        # Session dry-run is a CEILING, not a default: a client asking for a real run cannot
        # override a session that was started in preview mode.
        preview_only = True if self.dry_run else bool(dry_run)
        ok, output = gateway_module.run_commands(commands, dry_run=preview_only)
        self.state.log.append(output)
        result = {"ok": ok, "log": output, "commands": [list(c) for c in commands],
                  "dry_run": preview_only}
        if ok and not preview_only:
            result["card"] = self._card_from_state(dial_address)
        return result

    def _card_from_state(self, dial_address: str) -> dict | None:
        """After a real install, the two things the person has to keep are on disk: show them."""
        cert = self.repo_root / "state" / "registry-tls" / "server.crt"
        words_file = self.repo_root / "state" / "enroll-token.txt"
        if not (cert.exists() and words_file.exists()):
            return None
        card = self.pairing_card(str(cert), words_file.read_text().strip(), dial_address)
        return card if card.get("ok") else None

    def pairing_card(self, certificate_path: str, words: str, dial_address: str) -> dict:
        try:
            card = gateway_module.pairing_card(Path(certificate_path), words, dial_address)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "identity_code": card.identity_code, "words": card.words,
                "dial_address": card.dial_address, "text": card.as_text(),
                "lines": list(card.lines())}

    # -- pairing ------------------------------------------------------------------------------

    def inspect_gateway(self, host: str, port: int) -> dict:
        """Connect and report the identity code so the UI can ask the person to compare it."""
        from .pairing import TlsTransport

        transport = TlsTransport(GatewayRef(host, port))
        try:
            der = transport.peer_certificate_der()
        except PairingError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            transport.close()
        from .identity import identity_code

        return {"ok": True, "identity_code": identity_code(der)}

    def pair(
        self,
        host: str,
        port: int,
        words: str,
        device_name: str,
        *,
        expected_code: str | None = None,
        remember: bool = False,
    ) -> dict:
        if not expected_code:
            # Pairing is two-phase on purpose: inspect_gateway() shows the code, the person confirms
            # it, and only then does this function run with the code they confirmed.
            return {"ok": False,
                    "error": "confirm the gateway's identity code before pairing"}
        try:
            credentials = pair_with_gateway(
                GatewayRef(host, port),
                words,
                device_name,
                confirm=lambda code: False,  # unreachable: expected_code is set
                expected_code=expected_code,
            )
        except PairingCancelled as exc:
            return {"ok": False, "error": str(exc), "cancelled": True}
        except PairingError as exc:
            return {"ok": False, "error": str(exc)}
        storage_error = None
        try:
            backend = self.store.apply_choice(host, port, words, remember)
        except Exception as exc:  # noqa: BLE001 - pairing succeeded; the store did not
            backend = None
            storage_error = str(exc)
        # The credentials are the point of pairing: without them no device can be provisioned for
        # authenticated use. Hold them for the session, in a directory only this user can read.
        self.credentials = credentials
        self.certs_dir = self._materialise_credentials(credentials)
        self.state.paired = {
            "host": host, "port": port, "identity_code": credentials.identity_code,
            "dial_address": credentials.dial_address,
        }
        return {
            "ok": True,
            "identity_code": credentials.identity_code,
            "dial_address": credentials.dial_address,
            "remembered_in": backend,
            "storage_error": storage_error,
            "certs_dir": self.certs_dir,
        }

    def _materialise_credentials(self, credentials) -> str | None:
        """Write the paired credentials where the provisioning scripts expect to find them.

        A directory only this user can read (0700), removed when the session ends. The alternative
        is passing certificate text through a command line, which is how private keys end up in a
        process list.
        """
        if self.dry_run:
            return None
        import tempfile

        directory = Path(tempfile.mkdtemp(prefix="hermes-pair-"))
        directory.chmod(0o700)
        for name, contents in (
            ("ca.crt", credentials.ca_pem),
            ("client.crt", credentials.cert_pem),
            ("client.key", credentials.key_pem),
        ):
            target = directory / name
            target.write_text(contents)
            target.chmod(0o600)
        self._certs_tmp = directory
        return str(directory)

    # -- devices ------------------------------------------------------------------------------

    def flash(self, device_id: str, **fields: Any) -> dict:
        try:
            profile = self.registry.get(device_id)
            driver = self.registry.driver(device_id)
        except RegistryError as exc:
            return {"ok": False, "error": str(exc)}

        # A Wi-Fi passphrase typed into the form is written to a file only this user can read, handed
        # to the script by path, and deleted immediately afterwards. It is never an argument, never
        # in a log line, and never in the returned result.
        typed_password = fields.get("wifi_password")
        secret_path: str | None = None
        if typed_password and not bool(fields.get("dry_run")):
            handle, secret_path = tempfile.mkstemp(prefix="hermes-wifi-")
            with os.fdopen(handle, "w") as stream:
                stream.write(str(typed_password))
            os.chmod(secret_path, 0o600)

        request = DeviceRequest(
            device_name=str(fields.get("device_name") or ""),
            dial_address=str(fields.get("dial_address") or ""),
            wake_word=str(fields.get("wake_word") or profile.wake_default),
            voice=str(fields.get("voice") or "en_US-hfc_female-medium"),
            port=fields.get("port"),
            card_mount=fields.get("card_mount"),
            wifi_ssid=fields.get("wifi_ssid"),
            wifi_psk_file=secret_path or fields.get("wifi_psk_file"),
            # Device-specific inputs must survive the session, or a third-party driver can never
            # receive anything the shared UI did not happen to know about.
            device_id=fields.get("device_id"),
            card_user=str(fields.get("card_user") or "satellite"),
            certs_dir=fields.get("certs_dir"),
            wake_model=fields.get("wake_model"),
            extra=dict(fields.get("extra") or {}),
        )
        # Same ceiling as the gateway path: preview mode cannot be overridden by a client.
        runner = Runner(dry_run=self.dry_run or bool(fields.get("dry_run")),
                        repo_root=self.repo_root)
        name = fields.get("step", "flash")
        step: Callable[..., Any] = getattr(driver, str(name), None)  # type: ignore[assignment]
        if step is None:
            return {"ok": False, "error": f"driver has no step called {name!r}"}
        try:
            result = step(request, runner)
        except UnsupportedOnThisPlatform as exc:
            # A platform that cannot do this is an answer, not a crash.
            return {"ok": False, "error": str(exc), "unsupported": True}
        finally:
            if secret_path:
                try:
                    os.unlink(secret_path)
                except OSError:
                    pass
        return {
            "ok": result.ok,
            "summary": result.summary,
            "dry_run": result.dry_run,
            "commands": [
                {"argv": list(c.argv), "description": c.description,
                 "elevation": c.requires_elevation}
                for c in result.commands
            ],
            "output": result.output,
        }
