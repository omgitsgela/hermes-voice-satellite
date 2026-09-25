#!/usr/bin/env python3
"""Copy an existing NetworkManager Wi-Fi profile on to a satellite SD card.

Run this on the machine holding the card, as root, because two things require it: the source profile is
root-readable only, and NetworkManager ignores a system connection file that is not owned by root with
mode 0600. The point of copying an existing profile rather than retyping one is that **the Wi-Fi password
never leaves the machine**: it goes from NetworkManager's store into the card's store, and nobody has to
read it, paste it into a chat, or write it into a shell command.

    sudo python3 add-wifi-profile-to-card.py \
        --mount /run/media/<user>/rootfs \
        --source "GL-X750-360" \
        --name travel-router \
        --priority 5 \
        --home-profile preconfigured --home-priority 10

What it does:

- finds the source profile by its `[connection] id` (not by filename, which can differ),
- strips laptop-specific settings that would stop the Pi from ever activating it (`interface-name`,
  `mac-address`, `cloned-mac-address`),
- gives the copy a fresh UUID, its own id, `autoconnect=true`, and a priority,
- leaves `[wifi-security]` untouched, so the password and its flags carry across exactly,
- optionally raises the home profile's priority so the home network wins when both are in range,
- prints the SSID and the presence of a credential, never the credential itself.

Safe to re-run: the target file is replaced, and priorities are set to the values given.
"""

from __future__ import annotations

import argparse
import configparser
import pathlib
import re
import sys
import uuid


def parse_keyfile(path: pathlib.Path) -> configparser.ConfigParser:
    """A parser configured for NetworkManager keyfiles.

    Two things matter and both are silent if wrong: keys are case-sensitive, and interpolation must be
    off, or a Wi-Fi password containing `%` is written back doubled (`%%`) and the device simply cannot
    join. This cost nothing to get right and would have been baffling to debug at the other end.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path)
    return parser


#: Where a machine may keep its system connections. `/etc` is the classic location, but a machine managed
#: by netplan renders its profiles into `/run/NetworkManager/system-connections/` instead and leaves `/etc`
#: empty, which is exactly what this script tripped over on 2026-09-24. `--source-store` overrides these.
DEFAULT_STORES = (
    "/etc/NetworkManager/system-connections",
    "/run/NetworkManager/system-connections",
    "/var/lib/NetworkManager/system-connections",
)


def iter_profiles(stores: list[str]):
    for store in stores:
        path = pathlib.Path(store)
        if path.is_dir():
            yield from sorted(path.glob("*.nmconnection"))


def find_profile_file(stores: list[str], profile_id: str) -> pathlib.Path:
    """Find a profile by its `[connection] id`, its filename, or its SSID.

    All three, because a filename is not authoritative, and because the caller knows the profile by the name
    NetworkManager shows them, not by which of those three fields it lives in.
    """
    candidates: list[tuple[pathlib.Path, str, str]] = []
    unreadable: list[tuple[pathlib.Path, str]] = []
    for path in iter_profiles(stores):
        try:
            parser = parse_keyfile(path)
        except (configparser.Error, OSError, UnicodeDecodeError, ValueError) as exc:
            unreadable.append((path, type(exc).__name__))
            continue
        connection_id = parser.get("connection", "id", fallback="") if parser.has_section("connection") else ""
        ssid = parser.get("wifi", "ssid", fallback="").strip('"') if parser.has_section("wifi") else ""
        candidates.append((path, connection_id, ssid))
        if connection_id == profile_id or profile_id in path.name or (ssid and ssid == profile_id):
            return path

    # Not found: list what was there, so this is diagnosable without another round trip through a human.
    print(f"FAIL: no profile matching {profile_id!r} in {', '.join(stores)}", file=sys.stderr)
    if candidates:
        print("  profiles that were found:", file=sys.stderr)
        for path, connection_id, ssid in candidates:
            print(f"    {path.name}\n      id={connection_id!r} ssid={ssid!r}", file=sys.stderr)
    else:
        print("  no *.nmconnection files were found in those directories at all", file=sys.stderr)
    for path, why in unreadable:
        print(f"    {path.name}: unreadable ({why}); are you running as root?", file=sys.stderr)
    raise SystemExit(2)


def strip_to_blank(parser: configparser.ConfigParser, section: str, key: str) -> None:
    if parser.has_section(section) and parser.has_option(section, key):
        parser.remove_option(section, key)


def rewrite(parser: configparser.ConfigParser, *, name: str, priority: int) -> None:
    # `type` is required by NetworkManager and must survive; these are the laptop-specific keys that would
    # stop the Pi ever activating the profile.
    for key in ("interface-name", "mac-address", "cloned-mac-address", "master", "slave-type"):
        strip_to_blank(parser, "connection", key)
    parser.set("connection", "id", name)
    parser.set("connection", "uuid", str(uuid.uuid4()))
    parser.set("connection", "autoconnect", "true")
    parser.set("connection", "autoconnect-priority", str(priority))


def set_priority(parser: configparser.ConfigParser, priority: int) -> None:
    if parser.has_section("connection"):
        parser.set("connection", "autoconnect-priority", str(priority))


def write_root_only(path: pathlib.Path, parser: configparser.ConfigParser) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        parser.write(handle, space_around_delimiters=False)
    # NetworkManager ignores a system connection file that is not root-owned 0600.
    path.chmod(0o600)
    try:
        import os

        os.chown(path, 0, 0)
    except (OSError, PermissionError) as exc:
        raise SystemExit(
            f"FAIL: could not set root ownership on {path}: {exc}. NetworkManager will ignore the file."
        ) from exc


def describe(path: pathlib.Path, parser: configparser.ConfigParser) -> str:
    ssid = ""
    if parser.has_section("wifi"):
        raw = parser.get("wifi", "ssid", fallback="")
        ssid = raw.strip('"')
    security = "none"
    if parser.has_section("wifi-security"):
        has_psk = parser.has_option("wifi-security", "psk")
        flags = parser.get("wifi-security", "psk-flags", fallback="0")
        key_mgmt = parser.get("wifi-security", "key-mgmt", fallback="?")
        security = f"key-mgmt={key_mgmt}, psk present={has_psk}, psk-flags={flags}"
    stat = path.stat()
    return (f"{path.name}: ssid={ssid!r} priority="
            f"{parser.get('connection', 'autoconnect-priority', fallback='-')} "
            f"mode={oct(stat.st_mode & 0o777)} uid={stat.st_uid} {security}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mount", required=True, help="mount point of the card's rootfs partition")
    ap.add_argument("--source", required=True, help="[connection] id of the profile to copy")
    ap.add_argument("--name", required=True, help="id to give the copy on the card")
    ap.add_argument("--priority", type=int, default=5, help="autoconnect priority for the copy (default 5)")
    ap.add_argument("--home-profile", default=None, help="existing card profile to give a priority (optional)")
    ap.add_argument("--home-priority", type=int, default=10, help="priority for --home-profile (default 10)")
    ap.add_argument("--source-store", action="append", default=None,
                    help="directory to search for the source profile; repeatable. Defaults to the standard "
                         "NetworkManager stores, which includes the /run directory a netplan-managed "
                         "machine renders its profiles into")
    args = ap.parse_args()

    mount = pathlib.Path(args.mount)
    store = mount / "etc/NetworkManager/system-connections"
    if not (mount / "etc/NetworkManager").is_dir():
        raise SystemExit(f"FAIL: {mount} does not look like a satellite rootfs (no /etc/NetworkManager)")

    stores = args.source_store or list(DEFAULT_STORES)
    source = find_profile_file(stores, args.source)
    parser = parse_keyfile(source)
    rewrite(parser, name=args.name, priority=args.priority)

    target = store / f"{args.name}.nmconnection"
    write_root_only(target, parser)
    print(f"  wrote {describe(target, parser)}")

    if args.home_profile:
        home_path = store / f"{args.home_profile}.nmconnection"
        if not home_path.is_file():
            print(f"  note: no {home_path.name} on the card; skipped its priority")
        else:
            home = parse_keyfile(home_path)
            set_priority(home, args.home_priority)
            write_root_only(home_path, home)
            print(f"  wrote {describe(home_path, home)}")

    print("\n  profiles now on the card:")
    for path in sorted(store.glob("*.nmconnection")):
        stat = path.stat()
        print(f"    {path.name:<40} {oct(stat.st_mode & 0o777)} uid={stat.st_uid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
