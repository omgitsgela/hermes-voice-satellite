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
import urllib.parse

import hermes_gateway as gateway_module

from .driver import DeviceRequest, Runner, UnsupportedOnThisPlatform
from .pairing import GatewayRef, PairingCancelled, PairingError, pair as pair_with_gateway
from .registry import DeviceRegistry, RegistryError
from .secrets import CredentialStore


#: The parts of the checkout an installation actually runs. A bundle has to carry all of them.
#: Everything an installation actually reads. The list is deliberately wider than "the scripts":
#: a bundle that ships the engine but not the service it installs passes a scripts-only check and
#: then fails on a real install.
RUNTIME_ASSETS = (
    "scripts/install-gateway.sh",
    "scripts/install-esp32.sh",
    "scripts/deploy-to-card.sh",
    "scripts/esp32_provision.py",
    "scripts/issue-registry-tls.py",
    "scripts/add-wifi-profile-to-card.py",
    "firmware/esp32/models/hey_hermes.tflite",
    # The service the host install installs, and the unit it registers it as.
    "satellite_gateway/config.py",
    "satellite_gateway/loop.py",
    "hermes/systemd/hermes-voice-satellite-gateway.service.in",
    # TLS provisioning reads the partition table to know where credentials may be written.
    "firmware/esp32/partitions.csv",
    "requirements.txt",
    "satellite-gateway.env.example",
)


def _resolve_repo_root(repo_root: Path | str | None) -> Path:
    """Find the tree the installer runs from, which is not the current directory in a bundle.

    A packaged installer starts wherever the person launched it. Its scripts, its device profiles and
    its firmware models live inside the bundle, so when the given path has no scripts the bundle's own
    directory is used instead.
    """
    import sys

    if repo_root is not None:
        candidate = Path(repo_root)
        if (candidate / "scripts").exists() or not getattr(sys, "frozen", False):
            return candidate

    frozen = getattr(sys, "frozen", False)
    if frozen:
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent / "_internal"))
        if (bundle / "scripts").exists():
            return bundle
    return Path(repo_root) if repo_root is not None else Path.cwd()


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
        self.repo_root = _resolve_repo_root(repo_root)
        self.registry = registry or DeviceRegistry()
        if registry is None:
            self.registry.discover()
        self.store = store or CredentialStore()
        self.dry_run = dry_run
        # Set by a successful pairing: the credentials themselves, and a private directory the
        # provisioning scripts can read them from.
        self.credentials = None
        self.certs_dir: str | None = None
        #: The directory currently in use, and every directory made this session, so close() removes
        #: all of them rather than only the newest.
        self._certs_tmp: Path | None = None
        self._certs_dirs: list[Path] = []
        self.state = SessionState()
        self.state.hermes_shape = gateway_module.detect_hermes()

    # -- what the first screen needs -----------------------------------------------------------

    def platform_report(self) -> dict:
        """What this machine can and cannot do, stated rather than discovered by failing.

        The builds run everywhere, but the device work is shell and systemd: Windows cannot flash, and
        a gateway needs systemd to supervise the gateway. Elevation is listed too, because a step that
        needs an administrator should say so before it is attempted.
        """
        import sys

        elevated = os.geteuid() == 0 if hasattr(os, "geteuid") else False
        can_flash = sys.platform != "win32"
        can_supervise = sys.platform == "linux"
        notes = []
        if not can_flash:
            notes.append("Flashing a device needs Linux or macOS: the device scripts are shell. On "
                         "Windows, run that part on the machine holding the device, or inside WSL.")
        if not can_supervise:
            notes.append("Supervising a gateway needs systemd, which this platform does not have.")
        if not elevated:
            notes.append("Some card and Wi-Fi steps need administrator rights and will ask for them.")
        return {"name": sys.platform, "can_flash": can_flash, "can_supervise": can_supervise,
                "elevated": elevated, "notes": notes}

    def status(self) -> dict:
        return {
            "platform": self.platform_report(),
            "hermes": self.state.hermes_shape,
            "hermes_explained": {
                "desktop": "Hermes Desktop found. The installer will add the voice Gateway and "
                           "configure Hermes for it.",
                "service": "A Hermes service install found. The installer will also own how the "
                           "Gateway is supervised.",
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

        from itertools import islice

        candidates: list[Path] = []
        if sys.platform == "win32":
            candidates = [Path(f"{letter}:\\") for letter in string.ascii_uppercase]
        else:
            home = Path.home()
            roots = [Path("/run/media") / home.name, Path("/media") / home.name,
                     Path("/media"), Path("/mnt")]
            if sys.platform == "darwin":
                roots.insert(0, Path("/Volumes"))
            for base in roots:
                if not base.is_dir():
                    continue
                try:
                    # Bounded on the way in: a directory can hold thousands of entries, and each
                    # probe stats a filesystem that may be a slow network mount.
                    for child in islice((c for c in base.iterdir() if c.is_dir()), 40):
                        candidates.append(child)
                except OSError:
                    continue
                if len(candidates) >= 80:
                    break
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
        from .environment import merge

        ok, output = gateway_module.run_commands(
            commands, dry_run=preview_only, cwd=self.repo_root, env=merge()
        )
        self.state.log.append(output)
        result = {"ok": ok, "log": output, "commands": [list(c) for c in commands],
                  "dry_run": preview_only}
        if ok and not preview_only:
            result["card"] = self._card_from_state(dial_address)
            unit = self._write_pairing_unit(dial_address, tls_port)
            if unit:
                result["pairing_unit"] = str(unit["path"])
                result["pairing_unit_instructions"] = list(unit["instructions"])
        return result

    def _write_pairing_unit(self, dial_address: str, tls_port: int) -> dict | None:
        """Leave a startable pairing service behind, because an endpoint nobody starts is not an end.

        Written as a systemd USER unit so it needs no root and lives with the rest of this user's
        services. The installer prints how to enable it rather than enabling it silently.
        """
        import sys

        from hermes_gateway.unit import UnitConfig, enable_instructions, write_unit

        target = Path.home() / ".config" / "systemd" / "user" / "hermes-pairing.service"
        try:
            written = write_unit(
                UnitConfig(
                    python=Path(sys.executable),
                    frozen=bool(getattr(sys, "frozen", False)),
                    state_dir=self.repo_root / "state",
                    dial_address=f"tls://{dial_address}:{tls_port}",
                    working_directory=self.repo_root,
                ),
                target,
            )
        except (OSError, ValueError):
            return None
        return {"path": written, "instructions": enable_instructions(written)}

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
        device_id: str | None = None,
        expected_code: str | None = None,
        remember: bool = False,
        replace: bool = False,
    ) -> dict:
        # `device_name` is the pet name a person chose; `device_id` is what the gateway issues the
        # certificate for, and what provisioning later resolves files by. They are not the same
        # thing, and conflating them produces a device the registry will not recognise.
        enroll_as = device_id or device_name
        if not expected_code:
            # Pairing is two-phase on purpose: inspect_gateway() shows the code, the person confirms
            # it, and only then does this function run with the code they confirmed.
            return {"ok": False,
                    "error": "confirm the gateway's identity code before pairing"}
        if self.dry_run:
            # A practice run must not ask the gateway to issue a certificate, and must not touch the
            # credential store. Pairing is not a preview-able operation: it changes the gateway.
            return {"ok": True, "practice": True, "identity_code": expected_code,
                    "dial_address": f"tls://{host}:{port}", "remembered_in": None,
                    "certs_dir": None, "storage_error": None}
        try:
            credentials = pair_with_gateway(
                GatewayRef(host, port),
                words,
                enroll_as,
                confirm=lambda code: False,  # unreachable: expected_code is set
                expected_code=expected_code,
                replace=replace,
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

        # One live credential directory at a time: pairing again means the previous credentials are
        # superseded, not kept around in a temporary directory nobody will clean.
        self._discard_certs_dir()
        directory = Path(tempfile.mkdtemp(prefix="hermes-pair-"))
        directory.chmod(0o700)
        # The names are not a style choice: provision-device-tls.py reads exactly these four, and
        # resolves the certificate and key by the device's own identity.
        device = credentials.device_id or credentials.device_id
        files = (
            ("client-ca.crt", credentials.ca_pem),
            (f"{device}.crt", credentials.cert_pem),
            (f"{device}.key", credentials.key_pem),
            ("server.crt", credentials.server_cert_pem),
        )
        for name, contents in files:
            if not contents:
                raise OSError(f"pairing did not return {name.replace('.crt', ' material')}")
            target = directory / name
            target.write_text(contents)
            target.chmod(0o600)
        if credentials.device_fingerprint:
            record = directory / "device-credentials.json"
            record.write_text(f'{{"{credentials.device_fingerprint}": "{device}"}}')
            record.chmod(0o600)

        self._certs_tmp = directory
        self._certs_dirs.append(directory)
        return str(directory)

    def _discard_certs_dir(self) -> None:
        import shutil

        for directory in getattr(self, "_certs_dirs", []):
            shutil.rmtree(directory, ignore_errors=True)
        self._certs_dirs = []
        self._certs_tmp = None
        self.certs_dir = None

    def close(self) -> None:
        """Remove every temporary credential directory. A private key must not outlive the session."""
        self._discard_certs_dir()

    # -- what can be chosen -------------------------------------------------------------------

    def wake_word_catalogue(self, fmt: str | None = None, query: str = "") -> dict:
        from . import catalogue

        try:
            entries = catalogue.wake_words(fmt, query)
        except catalogue.CatalogueError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "caveat": catalogue.METRIC_CAVEAT,
            "wake_words": [
                {"id": e.id, "word": e.word, "format": e.format, "first_party": e.first_party,
                 "recall": e.recall, "fa_per_hour": e.fa_per_hour, "note": e.note,
                 "licence": e.licence, "metrics_line": e.metrics_line}
                for e in entries
            ],
        }

    def voice_catalogue(self) -> dict:
        from . import catalogue

        return {
            "ok": True,
            "default": catalogue.default_voice().id,
            "voices": [
                {"id": v.id, "name": v.name, "language": v.language, "size_mb": v.size_mb,
                 "sample_url": catalogue.sample_path(v),
                 "bundled": catalogue.sample_path(v).startswith("/samples/"),
                 "default": v.default}
                for v in catalogue.voices()
            ],
            "note": "The default voices can be heard offline; every other voice plays its published "
                    "sample before anything large is downloaded.",
        }

    def community_wake_words(self, query: str, *, refresh: bool = True) -> dict:
        """Measurements for community models, read from the public API.

        Read-only on purpose: those models are downloadable only with an account, so this returns
        figures for comparison and says so, rather than offering a download it cannot perform.
        """
        from . import catalogue

        if not refresh:
            return {"ok": True, "results": [], "note": "live lookup disabled"}
        results = catalogue.refresh_measurements(query)
        return {
            "ok": True,
            "results": results,
            "caveat": catalogue.METRIC_CAVEAT,
            "note": ("These are community models. Their measurements are shown so you can compare; "
                     "downloading one needs an account on openwakeword.com, so the installer does not "
                     "fetch them. A model file you supply yourself works instead."),
        }

    # -- a voice of your own ------------------------------------------------------------------

    def _piper_executable(self) -> str | None:
        import shutil

        for candidate in (self.repo_root / ".venv" / "bin" / "piper",
                          self.repo_root / ".venv" / "Scripts" / "piper.exe"):
            if candidate.exists():
                return str(candidate)
        return shutil.which("piper")

    def voice_file_problems(self, path: str) -> list[str]:
        """A Piper voice is two files, and half of them is not a voice."""
        voice = Path(str(path or ""))
        if not voice:
            return ["Choose a Piper voice file."]
        if not voice.exists():
            return [f"{voice} does not exist on this computer."]
        if voice.suffix != ".onnx":
            return [f"{voice.name} is not a Piper model; the model ends in .onnx."]
        if not Path(str(voice) + ".json").exists():
            return [f"Its config is missing: {voice.name}.json has to sit beside it."]
        return []

    def preview_voice_file(self, path: str) -> dict:
        """Synthesise a line through a voice the person supplied, so they hear it before it is used."""
        problems = self.voice_file_problems(path)
        if problems:
            return {"ok": False, "error": problems[0]}
        if self.dry_run:
            return {"ok": True, "practice": True,
                    "note": "In a real run this would synthesise a line through the voice so you "
                            "could hear it before it is used."}
        piper = self._piper_executable()
        if not piper:
            return {"ok": False,
                    "error": "There is no piper executable on this computer, so this voice cannot be "
                             "previewed here. It can still be selected."}
        import subprocess

        previews = Path.home() / ".cache" / "hermes-voice-satellite" / "previews"
        previews.mkdir(parents=True, exist_ok=True)
        target = previews / f"{Path(path).stem}.wav"
        sentence = "The quick brown fox jumps over the lazy dog."
        try:
            finished = subprocess.run(
                [piper, "-m", str(path), "-c", str(path) + ".json", "-f", str(target)],
                input=sentence, text=True, capture_output=True, timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"piper could not run: {exc}"}
        if not target.exists() or target.stat().st_size == 0:
            detail = (finished.stderr or "").strip()[:200]
            return {"ok": False, "error": f"the voice produced no audio. {detail}"}
        return {"ok": True, "preview_url": f"/previews/{target.name}", "size": target.stat().st_size}

    def install_voice_file(self, path: str, *, apply: bool = True) -> dict:
        """Put a supplied voice where the gateway keeps its voices, and select it."""
        problems = self.voice_file_problems(path)
        if problems:
            return {"ok": False, "error": problems[0]}
        if self.dry_run:
            return {"ok": True, "practice": True,
                    "note": "In a real run this would copy the voice into the gateway's voices "
                            "directory and select it."}
        source = Path(path)
        destination = self.repo_root / "voices"
        destination.mkdir(parents=True, exist_ok=True)
        for suffix in ("", ".json"):
            origin = Path(str(source) + suffix)
            target = destination / (source.name + suffix)
            target.write_bytes(origin.read_bytes())
        note = self._set_gateway_voice(source.stem, destination / source.name)
        return {"ok": True, "voice": source.stem, "applied": apply, "apply_note": note}

    def download_voice(self, voice_id: str, *, apply: bool = False) -> dict:
        """Fetch a Piper voice, and optionally make it the gateway's voice.

        Two files are needed (the model and its config), and both are large, so this is a deliberate
        action rather than something that happens because a list was scrolled.
        """
        from . import catalogue

        voice = next((v for v in catalogue.voices() if v.id == voice_id), None)
        if voice is None:
            return {"ok": False, "error": f"no voice called {voice_id!r} in the catalogue"}
        if self.dry_run:
            # Downloading 60 MB and rewriting the gateway's voice are exactly the kind of change a
            # preview promises not to make.
            return {"ok": True, "practice": True, "voice": voice.id, "downloaded": [],
                    "applied": False,
                    "note": f"In a real run this would download {voice.size_mb} MB and select it."}

        # The service resolves a voice as <checkout>/voices/<VOICE_PIPER_VOICE>.onnx[.json]. A
        # download into a cache, or a VOICE_CONFIG key nothing reads, leaves the Gateway speaking
        # with the previous voice after its restart.
        voices_dir = self.repo_root / "voices"
        model = voices_dir / f"{voice.id}.onnx"
        config = voices_dir / f"{voice.id}.onnx.json"
        # The Hugging Face layout is <lang>/<locale>/<name>/<quality>/<name>.onnx, where the folder
        # uses an underscore and the file uses a hyphen. Deriving it once, from the id, keeps the
        # catalogue and the URL from drifting apart.
        language, rest = voice.id.split("-", 1)
        name, quality = rest.rsplit("-", 1)
        # The folder is the speaker name exactly as published: `hfc_female`, not `hfc/female`. The
        # slash-splitting version built a URL that 404s for both shipped default voices.
        folder = name
        base = (f"https://huggingface.co/rhasspy/piper-voices/resolve/main/"
                f"{language[:2]}/{language}/{folder}/{quality}/{voice.id}.onnx")

        downloaded = []
        for target, url in ((model, base), (config, base + ".json")):
            if target.exists():
                continue
            try:
                catalogue.download(url, target)
            except Exception as exc:  # noqa: BLE001 - a failed download is an answer
                return {"ok": False, "error": f"could not fetch {target.name}: {exc}"}
            downloaded.append(str(target))

        result = {"ok": True, "voice": voice.id, "model": str(model), "config": str(config),
                  "downloaded": downloaded, "applied": False}
        if apply:
            result["applied"] = True
            result["apply_note"] = self._set_gateway_voice(voice.id, model)
        return result

    def _set_gateway_voice(self, voice_id: str, model: Path) -> str:
        """Point this gateway at a voice, by editing the Gateway environment in place.

        The Gateway speaks with one voice today; per-device voices are issue #120. This sets the one it
        has, and says so rather than implying more.
        """
        env = self.repo_root / "satellite-gateway.env"
        if not env.exists():
            return "satellite-gateway.env was not found here, so the voice was downloaded but not selected"
        if not model.exists():
            return (f"the voice files are not in {model.parent}, so the Gateway cannot load them; "
                    f"nothing was selected")
        lines = env.read_text().splitlines()
        # VOICE_CONFIG used to be written here and read by nothing: the service derives the config
        # path from the voice name. Only the name selects a voice.
        wanted = {"VOICE_PIPER_VOICE": voice_id}
        seen = set()
        output = []
        for line in lines:
            key = line.split("=", 1)[0]
            if key in wanted and not line.lstrip().startswith("#"):
                output.append(f"{key}={wanted[key]}")
                seen.add(key)
            else:
                output.append(line)
        for key, value in wanted.items():
            if key not in seen:
                output.append(f"{key}={value}")
        env.write_text("\n".join(output) + "\n")
        return "satellite-gateway.env now selects this voice for the gateway; restart the Gateway to use it"

    def prepare_wake_word(self, fmt: str, model_id: str) -> dict:
        """Make a chosen wake word available locally, verifying any download before it is used."""
        from . import catalogue

        try:
            entry = catalogue.find(model_id, fmt)
        except catalogue.CatalogueError as exc:
            return {"ok": False, "error": str(exc)}

        if entry.first_party or not entry.source_url.startswith("http"):
            local = self.repo_root / entry.source_url
            if local.exists():
                return {"ok": True, "path": str(local), "word": entry.word, "downloaded": False}
            # A first-party entry that is provisioned by an installer rather than a file.
            return {"ok": True, "path": None, "word": entry.word, "downloaded": False,
                    "note": entry.note or "provided by the device installer"}

        suffix = Path(urllib.parse.urlparse(entry.source_url).path).suffix or ".tflite"
        target = Path.home() / ".cache" / "hermes-voice-satellite" / "models" / f"{entry.id}{suffix}"
        if target.exists():
            return {"ok": True, "path": str(target), "word": entry.word, "downloaded": False}
        try:
            catalogue.download(entry.source_url, target, expect_sha256=entry.sha256)
        except Exception as exc:  # noqa: BLE001 - a failed download is an answer
            return {"ok": False, "error": f"could not fetch {entry.id}: {exc}"}
        return {"ok": True, "path": str(target), "word": entry.word, "downloaded": True}

    # -- devices ------------------------------------------------------------------------------

    def device_identity(self, profile_id: str, *, port: str | None = None) -> dict:
        """Ask the device what it calls itself. Not the friendly name: the identity it reports."""
        import re

        result = self.flash(profile_id, step="verify", port=port,
                            dial_address="", device_name="identity", dry_run=self.dry_run)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error") or result.get("summary")}
        output = str(result.get("output") or "")
        found = re.findall(r"\b(?:esp32|pi)-[0-9a-zA-Z]{4,}\b", output)
        if not found:
            return {"ok": False,
                    "error": "the device did not report an identity; connect it and try again"}
        return {"ok": True, "device_id": found[0], "raw": output}

    def preflight(self, profile_id: str, **fields: Any) -> dict:
        """Everything the stages will need, checked BEFORE anything is written.

        A flash that succeeds and a staging step that then refuses leaves a half-configured device,
        which is worse than refusing at the start. Every reason to refuse is collected here, so a
        person sees the whole list rather than one problem per attempt.
        """
        try:
            profile = self.registry.get(profile_id)
        except RegistryError as exc:
            return {"ok": False, "problems": [str(exc)]}

        problems: list[str] = []
        if not str(fields.get("dial_address") or "").strip():
            problems.append("No gateway address yet: pair with the gateway before installing.")
        if not str(fields.get("device_name") or "").strip():
            problems.append("Give the device a name.")

        if profile.needs_card_reader:
            mount = str(fields.get("card_mount") or "")
            if not mount:
                problems.append("Choose the card location.")
            elif not (Path(mount) / "etc").is_dir():
                problems.append(f"{mount} has no etc directory, so it is not the card's root "
                                f"filesystem. Mount the system partition, not the small boot one.")
        else:
            if not fields.get("port"):
                problems.append("Choose the USB connection the device is on.")
            if not fields.get("device_id"):
                problems.append("Read the device's own identity first: the certificate is issued for "
                                "that identity, not for the name you chose.")
            # The board's provisioner stages the network and refuses without one, so this has to be
            # caught before the firmware is written.
            if not fields.get("wifi_ssid"):
                problems.append("The board needs a Wi-Fi network: its setup stages the network the "
                                "device will join, and cannot skip it.")
            elif not (fields.get("wifi_password") or fields.get("wifi_psk_file")):
                problems.append("A Wi-Fi network needs its password as well as its name.")

        model = str(fields.get("wake_model") or "")
        if model and not Path(model).exists() and not (self.repo_root / model).exists():
            problems.append(f"The wake-word file {model} does not exist on this computer.")
        if not model and profile.wake_catalogue_format() == "microwakeword_tflite":
            # The driver passes the firmware's bundled model by this path; if it is not here, the
            # device would silently fall back to a different phrase.
            bundled = self.repo_root / f"firmware/esp32/models/{profile.wake_default}.tflite"
            if not bundled.exists():
                problems.append(
                    f"The wake-word model for {profile.wake_default} is missing from this checkout "
                    f"({bundled}). Supply a model file instead."
                )

        certs = str(fields.get("certs_dir") or "")
        if certs and not Path(certs).is_dir():
            problems.append(f"The paired credentials are no longer at {certs}; pair again.")

        return {"ok": not problems, "problems": problems}

    def install_device(self, profile_id: str, **fields: Any) -> dict:
        """Flash the device, then stage what must be staged. Success means every stage succeeded."""
        try:
            self.registry.get(profile_id)
        except RegistryError as exc:
            return {"ok": False, "error": str(exc)}

        # Refuse before writing anything, with every reason at once.
        check = self.preflight(profile_id, **fields)
        if not check["ok"]:
            return {"ok": False, "problems": check["problems"],
                    "error": "; ".join(check["problems"]),
                    "summary": check["problems"][0] if check["problems"] else "not ready"}

        first = self.flash(profile_id, **fields)
        if not first.get("ok"):
            return first

        # A card's Wi-Fi is a saved network copied on; a cable device stages Wi-Fi, the dial address,
        # credentials and the model in its own provision step.
        wants_provision = bool(
            fields.get("wifi_ssid") or fields.get("certs_dir") or fields.get("device_id")
        )
        if not wants_provision:
            return first

        second = self.flash(profile_id, **{**fields, "step": "provision"})
        stages = [{"stage": "flash", **first}, {"stage": "provision", **second}]
        return {
            "ok": bool(first.get("ok")) and bool(second.get("ok")),
            "summary": (
                second.get("summary") if not second.get("ok")
                else f"{first.get('summary', '')}; {second.get('summary', '')}".strip("; ")
            ),
            "dry_run": first.get("dry_run", False) or second.get("dry_run", False),
            "commands": first.get("commands", []) + second.get("commands", []),
            "stages": stages,
            "output": "\n".join(filter(None, [first.get("output", ""), second.get("output", "")])),
        }

    def manage_device(self, profile_id: str, **fields: Any) -> dict:
        """Change what the cable can change on a device that is already set up. No flashing.

        Wi-Fi, the dial address, the credentials and the wake model live on the device or its card, so
        this stages them again. Volume, brightness and the wake threshold are gateway-side settings and
        are deliberately NOT pretended here: the gateway owns them and this installer cannot reach it
        yet.
        """
        try:
            self.registry.get(profile_id)
        except RegistryError as exc:
            return {"ok": False, "error": str(exc)}
        if not (fields.get("wifi_ssid") or fields.get("certs_dir") or fields.get("wake_model")):
            # The address is NOT required here: the device already has one, and re-typing it is not
            # a change. Only staging something new is.
            return {"ok": False,
                    "error": "nothing to change: choose a Wi-Fi network, a wake word, or pair again "
                             "to refresh the credentials"}
        result = self.flash(profile_id, **{**fields, "step": "provision"})
        result["mode"] = "manage"
        return result

    def flash(self, profile_id: str, **fields: Any) -> dict:
        try:
            profile = self.registry.get(profile_id)
            driver = self.registry.driver(profile_id)
        except RegistryError as exc:
            return {"ok": False, "error": str(exc)}

        # Same ceiling as the gateway path: preview mode cannot be overridden by a client, and it
        # decides whether a secret file is needed at all.
        preview = self.dry_run or bool(fields.get("dry_run"))
        name = str(fields.get("step", "flash"))
        step: Callable[..., Any] = getattr(driver, name, None)  # type: ignore[assignment]
        if step is None:
            return {"ok": False, "error": f"driver has no step called {name!r}"}

        # A Wi-Fi passphrase typed into the form is written to a file only this user can read, handed
        # to the script by path, and deleted immediately afterwards. It is never an argument, never
        # in a log line, and never in the returned result.
        typed_password = fields.get("wifi_password")
        secret_path: str | None = None
        if typed_password and not preview:
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
            registry_file=fields.get("registry_file") or (
                str(Path(fields["certs_dir"]) / "device-credentials.json")
                if fields.get("certs_dir") and
                (Path(fields["certs_dir"]) / "device-credentials.json").exists() else None
            ),
            wake_model=fields.get("wake_model"),
            extra=dict(fields.get("extra") or {}),
        )
        runner = Runner(dry_run=preview, repo_root=self.repo_root)
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
