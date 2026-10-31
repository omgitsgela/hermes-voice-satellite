#!/usr/bin/env python3
"""Check the Gateway's *wired* registry path on this host: config -> credential loader -> listener -> device.

The offline suites drive `satellite_gateway/registry.py` and `pi/outbound_satellite.py` directly. This one goes through
`satellite_gateway/config.py` and the same sequence `satellite_gateway/loop.py` uses when it starts the listener, so a change to a
config key, to the credential file format, or to the failure policy shows up here rather than in production.

It binds a temporary port, writes a temporary credentials file, and connects a fake device. It does not touch
the satellite, the live listener port, or any service.

    ~/hermes-voice-satellite/.venv/bin/python scripts/check-registry-wiring.py
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wyoming.event import Event, async_read_event, async_write_event  # noqa: E402

from satellite_gateway.config import (  # noqa: E402
    REGISTRY_ALLOW_UNAUTHENTICATED,
    REGISTRY_CREDENTIALS_FILE,
    REGISTRY_DUPLICATE_POLICY,
    REGISTRY_ENABLED,
    REGISTRY_HOST,
    REGISTRY_PORT,
)
from satellite_gateway.registry import (  # noqa: E402
    REGISTER_EVENT,
    REGISTER_OK_EVENT,
    REGISTER_REFUSED_EVENT,
    CredentialConfigError,
    RegistryServer,
    SatelliteRegistry,
    load_credential_lookup,
)

PORT = 10797
RESULTS: list[tuple[bool, str]] = []


def report(ok: bool, what: str) -> None:
    RESULTS.append((ok, what))
    print(f"  {'ok  ' if ok else 'FAIL'}  {what}")


async def register(port: int, device_id: str, registry: SatelliteRegistry) -> tuple[str, list[str]]:
    """Connect, answer describe, claim an identity.

    Returns the reply event type and the registry's view of who is connected *at that moment*, sampled
    before the socket closes: a device that disconnects is correctly removed from the registry, so checking
    afterwards would always see an empty list.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        first = await asyncio.wait_for(async_read_event(reader), 3)
        assert first is not None and first.type == "describe", f"expected describe, got {first}"
        await async_write_event(
            Event(type="info", data={"satellite": {"name": "bench", "area": "bench"}}), writer
        )
        await async_write_event(
            Event(type=REGISTER_EVENT, data={
                "device_id": device_id, "name": "bench", "area": "bench",
                "protocol_version": 1, "capabilities": ["led", "speaker"], "boot_id": "bench-boot",
            }),
            writer,
        )
        await writer.drain()
        reply = await asyncio.wait_for(async_read_event(reader), 3)
        connected = registry.connected_devices()
        return (reply.type if reply is not None else "closed"), connected
    finally:
        writer.close()


async def main() -> int:
    print("=== 1. the configuration the Gateway would read ===")
    print(f"  enabled={REGISTRY_ENABLED} host={REGISTRY_HOST} port={REGISTRY_PORT}")
    print(f"  duplicate policy={REGISTRY_DUPLICATE_POLICY}")
    print(f"  credentials file={REGISTRY_CREDENTIALS_FILE}")
    print(f"  unauthenticated opt-in={REGISTRY_ALLOW_UNAUTHENTICATED}")
    if os.environ.get("VOICE_REGISTRY_ENABLED"):
        # Deliberately enabled: report it rather than failing, because that is an operator's decision.
        report(True, "the listener is enabled by VOICE_REGISTRY_ENABLED in this environment")
    else:
        report(not REGISTRY_ENABLED, "the listener is off by default (enabling it is a deliberate act)")

    with tempfile.TemporaryDirectory() as tmp:
        creds = pathlib.Path(tmp) / "device-credentials.json"

        print("\n=== 2. a credential file that maps the transport's peer identity ===")
        # Over plain TCP the peer identity is the literal 'unauthenticated'. Mapping it here exercises the
        # real loader and the real match-and-refuse path end to end, which is what makes this check worth
        # having: it fails if the config key, the file format or the lookup wiring drifts.
        creds.write_text(json.dumps({"unauthenticated": "bench-1"}))
        lookup = load_credential_lookup(creds)
        report(lookup is not None and lookup("unauthenticated") == "bench-1",
               "the loader maps a fingerprint to a device id")

        registry = SatelliteRegistry(duplicate_policy=REGISTRY_DUPLICATE_POLICY)
        server = RegistryServer(registry, "127.0.0.1", PORT, credential_lookup=lookup)
        await server.start()
        try:
            accepted, seen = await register(PORT, "bench-1", registry)
            report(accepted == REGISTER_OK_EVENT, f"a matching identity registers ({accepted})")
            report(seen == ["bench-1"], f"the registry holds the accepted device while it is connected ({seen})")
            refused, seen_after = await register(PORT, "bench-2", registry)
            report(refused == REGISTER_REFUSED_EVENT, f"a mismatched identity is refused ({refused})")
            report(seen_after == [], f"a refused device leaves no slot behind ({seen_after})")
        finally:
            await server.stop()

        print("\n=== 3. failure policy: broken configuration must not open the listener ===")
        creds.write_text("{ not json at all")
        try:
            load_credential_lookup(creds, allow_unauthenticated=True)
            report(False, "a malformed credential file refuses even with the opt-in set")
        except CredentialConfigError as exc:
            report("unusable" in str(exc), "a malformed credential file refuses with a usable reason")

        missing = pathlib.Path(tmp) / "absent.json"
        try:
            load_credential_lookup(missing)
            report(False, "a missing credential file refuses without an explicit opt-in")
        except CredentialConfigError:
            report(True, "a missing credential file refuses without an explicit opt-in")
        report(load_credential_lookup(missing, allow_unauthenticated=True) is None,
               "a missing credential file is accepted only with the explicit opt-in")

    failures = [what for ok, what in RESULTS if not ok]
    print(f"\nregistry wiring checks: {len(RESULTS) - len(failures)}/{len(RESULTS)} passed")
    for what in failures:
        print(f"  FAIL: {what}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
