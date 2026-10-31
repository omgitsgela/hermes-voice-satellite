"""Finding device profiles and their drivers.

A device package declares its profile and its driver through the
``hermes_installer.devices`` entry point, so the installer discovers third-party devices without
importing anything by name. That is what makes "add a device" a package and a file rather than an
edit inside the installer.
"""
from __future__ import annotations

from importlib.metadata import entry_points
from typing import Iterable

from .profile import DeviceProfile

ENTRY_POINT_GROUP = "hermes_installer.devices"


class RegistryError(Exception):
    pass


class DeviceRegistry:
    """Profiles by id, plus the drivers that implement them."""

    def __init__(self) -> None:
        self._profiles: dict[str, DeviceProfile] = {}
        self._drivers: dict[str, object] = {}
        self.load_errors: list[str] = []

    # -- registration -------------------------------------------------------------------------

    def register(self, profile: DeviceProfile, driver: object | None = None) -> None:
        if profile.id in self._profiles:
            raise RegistryError(f"two device profiles share the id {profile.id!r}")
        self._profiles[profile.id] = profile
        if driver is not None:
            self._drivers[profile.id] = driver

    #: First-party device modules, tried before entry points so a checkout works without an
    #: install step. A device package found by entry point takes the normal path.
    FIRST_PARTY = ("hermes_device_esp32", "hermes_device_pi")

    def discover(self) -> int:
        """Load every installed device package. Returns how many were registered.

        A broken third-party package is recorded in `load_errors` rather than taking the installer
        down with it: one bad plugin must not stop a person setting up their house.
        """
        loaded = 0
        for module_name in self.FIRST_PARTY:
            try:
                module = __import__(module_name)
            except ImportError:
                continue
            profile = getattr(module, "PROFILE", None)
            if profile is not None and profile.id not in self._profiles:
                self.register(profile, driver=getattr(module, "DRIVER", None))
                loaded += 1
        for entry in entry_points(group=ENTRY_POINT_GROUP):
            try:
                loaded_object = entry.load()
                profile = getattr(loaded_object, "PROFILE", None)
                if profile is None:
                    raise RegistryError(f"{entry.name}: no PROFILE")
                self.register(profile, driver=getattr(loaded_object, "DRIVER", None))
                loaded += 1
            except Exception as exc:  # noqa: BLE001 - one bad plugin must not be fatal
                self.load_errors.append(f"{entry.name}: {exc}")
        return loaded

    # -- lookup -------------------------------------------------------------------------------

    def all(self) -> tuple[DeviceProfile, ...]:
        return tuple(sorted(self._profiles.values(), key=lambda p: p.display_name.lower()))

    def get(self, device_id: str) -> DeviceProfile:
        try:
            return self._profiles[device_id]
        except KeyError:
            known = ", ".join(sorted(self._profiles)) or "none"
            raise RegistryError(f"unknown device {device_id!r}; known devices: {known}") from None

    def driver(self, device_id: str) -> object:
        try:
            return self._drivers[device_id]
        except KeyError:
            raise RegistryError(
                f"device {device_id!r} has a profile but no driver, so it cannot be flashed yet"
            ) from None

    def __len__(self) -> int:
        return len(self._profiles)

    def __iter__(self) -> Iterable[DeviceProfile]:
        return iter(self.all())
