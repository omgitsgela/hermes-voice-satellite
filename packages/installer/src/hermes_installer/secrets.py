"""Remembering the pairing words, only when a person ticks the box.

Default is forget: the words live in the installer's memory for the session and are never written.
When the box is ticked the OS keyring is used if one is available (Windows Credential Manager,
libsecret, KWallet), because that is the right place for a credential. When it is not available the
fallback is a `0600` file, and the caller is told which backend was used so the UI can say so
plainly instead of implying a keyring that is not there.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Protocol

SERVICE = "hermes-voice-installer"


class StoreError(Exception):
    pass


class Backend(Protocol):
    name: str

    def save(self, account: str, secret: str) -> None: ...

    def load(self, account: str) -> str | None: ...

    def forget(self, account: str) -> None: ...


class KeyringBackend:
    """The OS credential store. Imported lazily so the installer runs without the dependency."""

    name = "os keyring"

    def __init__(self) -> None:
        import keyring  # noqa: F401 - presence is the point

        self._keyring = keyring

    def save(self, account: str, secret: str) -> None:
        self._keyring.set_password(SERVICE, account, secret)

    def load(self, account: str) -> str | None:
        return self._keyring.get_password(SERVICE, account)

    def forget(self, account: str) -> None:
        try:
            self._keyring.delete_password(SERVICE, account)
        except Exception:  # noqa: BLE001 - absence is the desired end state
            pass


class FileBackend:
    """A `0600` file. Honest about being a file."""

    name = "a protected file (0600)"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()

    def _read(self) -> dict[str, str]:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError(f"could not read {self.path}: {exc}") from exc

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".cred-")
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(data, stream)
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary, self.path)
        except OSError as exc:
            raise StoreError(f"could not write {self.path}: {exc}") from exc

    def save(self, account: str, secret: str) -> None:
        data = self._read()
        data[account] = secret
        self._write(data)

    def load(self, account: str) -> str | None:
        return self._read().get(account)

    def forget(self, account: str) -> None:
        data = self._read()
        if data.pop(account, None) is not None:
            self._write(data)


def default_path() -> Path:
    """Where the fallback file lives, per platform, without assuming a home directory name."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "hermes-installer" / "credentials.json"


def account_for(host: str, port: int) -> str:
    return f"{host}:{port}"


class CredentialStore:
    """Per-gateway remembering, with the choice made explicit by the caller."""

    def __init__(self, backend: Backend | None = None) -> None:
        if backend is not None:
            self.backend = backend
        else:
            try:
                self.backend = KeyringBackend()
            except Exception:  # noqa: BLE001 - no keyring is normal, not an error
                self.backend = FileBackend()

    @property
    def backend_name(self) -> str:
        return self.backend.name

    def remember(self, host: str, port: int, words: str) -> str:
        """Save the words and return the backend name, so the UI can say where they went."""
        self.backend.save(account_for(host, port), words)
        return self.backend.name

    def recall(self, host: str, port: int) -> str | None:
        return self.backend.load(account_for(host, port))

    def forget(self, host: str, port: int) -> None:
        """Also the path taken when the box was left unticked, so a stale secret cannot linger."""
        self.backend.forget(account_for(host, port))

    def apply_choice(self, host: str, port: int, words: str, remember: bool) -> str | None:
        """The single call the UI makes: tick means save, untick means make sure nothing is saved."""
        if remember:
            return self.remember(host, port, words)
        self.forget(host, port)
        return None
