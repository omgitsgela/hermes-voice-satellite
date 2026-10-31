"""One session object, two front ends.

The console flow and the web UI drive the same object, so a step that works in one works in the
other, and a test can drive it with no interface at all. Nothing here decides what a device is: it
asks the registry, and the registry asks the device packages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import hermes_gateway as gateway_module

from .driver import DeviceRequest, Runner
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
        preview_only = self.dry_run if dry_run is None else dry_run
        ok, output = gateway_module.run_commands(commands, dry_run=preview_only)
        self.state.log.append(output)
        return {"ok": ok, "log": output, "commands": [list(c) for c in commands],
                "dry_run": preview_only}

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
        backend = self.store.apply_choice(host, port, words, remember)
        self.state.paired = {
            "host": host, "port": port, "identity_code": credentials.identity_code,
            "dial_address": credentials.dial_address,
        }
        return {
            "ok": True,
            "identity_code": credentials.identity_code,
            "dial_address": credentials.dial_address,
            "remembered_in": backend,
        }

    # -- devices ------------------------------------------------------------------------------

    def flash(self, device_id: str, **fields: Any) -> dict:
        try:
            profile = self.registry.get(device_id)
            driver = self.registry.driver(device_id)
        except RegistryError as exc:
            return {"ok": False, "error": str(exc)}

        request = DeviceRequest(
            device_name=str(fields.get("device_name") or ""),
            dial_address=str(fields.get("dial_address") or ""),
            wake_word=str(fields.get("wake_word") or profile.wake_default),
            voice=str(fields.get("voice") or "en_US-hfc_female-medium"),
            port=fields.get("port"),
            card_mount=fields.get("card_mount"),
            wifi_ssid=fields.get("wifi_ssid"),
            wifi_psk_file=fields.get("wifi_psk_file"),
            certs_dir=fields.get("certs_dir"),
            wake_model=fields.get("wake_model"),
        )
        runner = Runner(dry_run=bool(fields.get("dry_run", self.dry_run)))
        name = fields.get("step", "flash")
        step: Callable[..., Any] = getattr(driver, str(name), None)  # type: ignore[assignment]
        if step is None:
            return {"ok": False, "error": f"driver has no step called {name!r}"}
        result = step(request, runner)
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
