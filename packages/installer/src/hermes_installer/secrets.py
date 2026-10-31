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
import secrets as _secrets
import stat
import sys
import tempfile
import threading
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
    """The OS credential store. Imported lazily so the installer runs without the dependency.

    Importable is not the same as usable: a locked keyring or a session without a secret service
    raises on the first write. Probing once here is what lets the store fall back quietly instead of
    failing halfway through pairing.
    """

    name = "your operating system's password store"

    def __init__(self) -> None:
        import keyring

        self._keyring = keyring
        probe = f"probe-{_secrets.token_hex(8)}"
        self._keyring.set_password(SERVICE, probe, "ok")
        try:
            self._keyring.delete_password(SERVICE, probe)
        except Exception:  # noqa: BLE001 - a leftover probe entry is harmless
            pass

    def save(self, account: str, secret: str) -> None:
        self._keyring.set_password(SERVICE, account, secret)

    def load(self, account: str) -> str | None:
        return self._keyring.get_password(SERVICE, account)

    def forget(self, account: str) -> None:
        """Delete, and say so if the delete failed. Silently swallowing this claimed a success the
        store had not achieved, leaving a secret behind after a person asked for it to be removed."""
        try:
            self._keyring.delete_password(SERVICE, account)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "PasswordDeleteError":
                return  # nothing was stored, which is the desired end state
            raise StoreError(f"could not remove the saved words: {exc}") from exc


_FILE_LOCK = threading.RLock()


class FileBackend:
    """A file only this user should be able to read. Honest about what that means per platform.

    `chmod(0600)` is not a Windows ACL. Rather than advertise protection the file does not have, the
    Windows path restricts the file with `icacls`, and the label says which of the two actually
    happened.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self._windows_restricted: bool | None = None

    @property
    def name(self) -> str:
        if sys.platform == "win32":
            if self._windows_restricted:
                return "a file restricted to your Windows account"
            return "a file (its Windows permissions could not be restricted)"
        return "a file only this user can read (0600)"

    def _restrict_windows(self) -> None:
        """Remove inherited access and grant only the current account. Best effort, and recorded."""
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        if not user:
            self._windows_restricted = False
            return
        try:
            import subprocess

            result = subprocess.run(
                ["icacls", str(self.path), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True, text=True, timeout=30,
            )
            self._windows_restricted = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            self._windows_restricted = False

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
            if sys.platform == "win32":
                self._restrict_windows()
        except OSError as exc:
            raise StoreError(f"could not write {self.path}: {exc}") from exc

    def save(self, account: str, secret: str) -> None:
        # The whole read-modify-write must hold the lock, or a concurrent forget can be undone by a
        # save that read the file before the delete and writes its stale copy afterwards.
        with _FILE_LOCK:
            data = self._read()
            data[account] = secret
            self._write(data)

    def load(self, account: str) -> str | None:
        with _FILE_LOCK:
            return self._read().get(account)

    def forget(self, account: str) -> None:
        with _FILE_LOCK:
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
        # Raises StoreError if the removal genuinely failed; the caller reports that rather than
        # telling somebody their words were forgotten when they were not.
        self.forget(host, port)
        return None
