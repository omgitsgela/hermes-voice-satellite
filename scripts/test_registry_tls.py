#!/usr/bin/env python3
"""Offline tests for TLS on the registry listener (issue #61, the transport decision).

Run with the Gateway's own interpreter:

    ~/hermes-voice-satellite/.venv/bin/python scripts/test_registry_tls.py

TLS terminates inside the gateway, and the registration credential is the fingerprint of the device's
client certificate, so these checks are real handshakes between real sockets on localhost using the
throwaway certificates in `scripts/test_tls_fixtures/`. Those are test material only: they are
regenerated as a set (see the README beside them) and no deployment uses them.

Checks:

  1  no certificate configured -> no context: the listener stays a plain local socket
  2  half a key pair is refused, and so is a certificate with no client CA
  3  the context requires a client certificate and refuses anything below TLS 1.2
  4  a listed certificate registers as the identity its fingerprint maps to
  5  a certificate from a CA the listener does not trust never completes the handshake
  6  a certificate from the trusted CA that the credential file does not list is refused
  7  plaintext against a TLS listener registers nothing and is not given a conversation
  8  the certificate paths are configurable and default beside the Gateway's other state
  9  the Gateway builds the listener's context from those paths, and treats a bad pair as fatal
 10  over TLS, a payload claiming another identity is still refused
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import pathlib
import shutil
import ssl
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = pathlib.Path(__file__).resolve().parent
FIXTURES = SCRIPTS / "test_tls_fixtures"

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCRIPTS))

from wyoming.event import async_read_event  # noqa: E402

from gateway.registry import (  # noqa: E402
    REGISTER_OK_EVENT,
    REGISTER_REFUSED_EVENT,
    RegistryServer,
    SatelliteRegistry,
    TlsConfigError,
    load_server_tls_context,
)
from test_registry import FakeDevice, free_port  # noqa: E402

TIMEOUT = 5.0
LISTED_DEVICE_ID = "tls-kitchen-1"


def fingerprint_of(cert_name: str) -> str:
    """The fingerprint the listener computes: SHA-256 over the DER the peer presents."""
    der = ssl.PEM_cert_to_DER_cert((FIXTURES / f"{cert_name}.crt").read_text())
    return "sha256:" + hashlib.sha256(der).hexdigest()


def server_context(*, client_ca: str = "ca.crt") -> ssl.SSLContext:
    context = load_server_tls_context(
        FIXTURES / "server.crt", FIXTURES / "server.key", FIXTURES / client_ca
    )
    assert context is not None, "the fixture pair should produce a context"
    return context


def client_context(cert: str | None, *, ca: str = "ca.crt") -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(FIXTURES / ca))
    if cert is not None:
        context.load_cert_chain(str(FIXTURES / f"{cert}.crt"), str(FIXTURES / f"{cert}.key"))
    return context


class TlsFakeDevice(FakeDevice):
    """The same fake satellite, dialling over TLS: connect() is the only difference."""

    def __init__(self, port: int, ssl_context: ssl.SSLContext | None) -> None:
        super().__init__(port)
        self.ssl_context = ssl_context

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", self.port, ssl=self.ssl_context, server_hostname="localhost"
        )
        first = await asyncio.wait_for(async_read_event(self.reader), TIMEOUT)
        self.describe_seen = bool(first and first.type == "describe")
        await self.send("info", {"satellite": {"name": "fake", "area": "test-room"}})


class TlsHarness:
    """A registry, a TLS listener on a free localhost port, and a way to close both."""

    def __init__(self, *, credential_lookup=None, ssl_context=None) -> None:
        self.registry = SatelliteRegistry()
        self.port = free_port()
        self.server = RegistryServer(
            self.registry, "127.0.0.1", self.port,
            credential_lookup=credential_lookup, ssl_context=ssl_context,
        )
        self.devices: list[TlsFakeDevice] = []

    async def start(self) -> None:
        await self.server.start()

    async def device(self, ssl_context: ssl.SSLContext | None) -> TlsFakeDevice:
        dev = TlsFakeDevice(self.port, ssl_context)
        await dev.connect()
        self.devices.append(dev)
        return dev

    async def stop(self) -> None:
        for dev in self.devices:
            await dev.close()
        await self.server.stop()
        await self.registry.close_all()


TESTS: list[tuple[str, str]] = []


def check(number: str, description: str):
    def wrap(fn):
        TESTS.append((number, description, fn))
        return fn
    return wrap


# ---------------------------------------------------------------- checks

@check("1", "no certificate configured means no context, and no TLS")
async def t_no_certificate_is_no_tls() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        assert load_server_tls_context(root / "server.crt", root / "server.key", root / "ca.crt") is None


@check("2", "half a key pair, or a certificate with no client CA, is refused")
async def t_partial_configuration_refuses() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        shutil.copyfile(FIXTURES / "server.crt", root / "server.crt")
        shutil.copyfile(FIXTURES / "server.key", root / "server.key")
        shutil.copyfile(FIXTURES / "ca.crt", root / "ca.crt")

        # A certificate with no key: someone deployed half of a pair. Refuse rather than serve plaintext.
        try:
            load_server_tls_context(root / "server.crt", root / "missing.key", root / "ca.crt")
            raise AssertionError("a certificate with no key must be refused")
        except TlsConfigError as exc:
            assert "key" in str(exc)

        try:
            load_server_tls_context(root / "missing.crt", root / "server.key", root / "ca.crt")
            raise AssertionError("a key with no certificate must be refused")
        except TlsConfigError as exc:
            assert "certificate" in str(exc)

        # Both, but no client CA: mutual TLS without the CA cannot verify a device, so it is not mutual TLS.
        try:
            load_server_tls_context(root / "server.crt", root / "server.key", root / "missing-ca.crt")
            raise AssertionError("a TLS listener without a client CA must be refused")
        except TlsConfigError as exc:
            assert "client CA" in str(exc)


@check("3", "the context requires a client certificate and refuses anything below TLS 1.2")
async def t_context_is_mutual_and_modern() -> None:
    context = server_context()
    assert context.verify_mode == ssl.CERT_REQUIRED, "a fingerprint credential needs the peer's certificate"
    # Stated rather than merely inherited: the ESP32 satellites speak mbedTLS, so 1.2 is the floor they
    # can meet. Deleting this line is currently undetectable, because Python's own default for a server
    # context is already TLS 1.2 — it is here to survive a change of that default, not to change the
    # behaviour today.
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2, "1.2 is the floor the ESP32 side can speak"
    assert context.check_hostname is False, "a server does not verify its own name"


@check("4", "a listed certificate registers as the identity its fingerprint maps to")
async def t_listed_certificate_registers() -> None:
    fingerprint = fingerprint_of("listed-client")
    h = TlsHarness(credential_lookup={fingerprint: LISTED_DEVICE_ID}.get, ssl_context=server_context())
    await h.start()
    try:
        dev = await h.device(client_context("listed-client"))
        assert dev.describe_seen, "the listener should ask Describe first over TLS too"
        reply = await dev.register(LISTED_DEVICE_ID)
        assert reply is not None and reply.type == REGISTER_OK_EVENT, f"unexpected reply {reply}"
        slot = h.registry.get(LISTED_DEVICE_ID)
        assert slot is not None and slot.ready
        assert h.registry.connected_devices() == [LISTED_DEVICE_ID]
    finally:
        await h.stop()


@check("5", "a certificate from a CA the listener does not trust never completes the handshake")
async def t_untrusted_client_ca_refused() -> None:
    fingerprint = fingerprint_of("listed-client")
    h = TlsHarness(credential_lookup={fingerprint: LISTED_DEVICE_ID}.get, ssl_context=server_context())
    await h.start()
    try:
        # The client trusts the server fine; it is the client's own certificate the listener rejects,
        # which is what stops a device the household never enrolled.
        try:
            await h.device(client_context("other-client"))
            raise AssertionError("a certificate from another CA must not complete the handshake")
        except (ssl.SSLError, ConnectionError, asyncio.IncompleteReadError, EOFError):
            pass
        await asyncio.sleep(0.1)
        assert len(h.registry) == 0, "nothing from that connection may be registered"
        assert h.registry.connected_devices() == []
    finally:
        await h.stop()


@check("6", "a trusted certificate the credential file does not list is refused")
async def t_unlisted_but_trusted_certificate_refused() -> None:
    fingerprint = fingerprint_of("listed-client")
    h = TlsHarness(credential_lookup={fingerprint: LISTED_DEVICE_ID}.get, ssl_context=server_context())
    await h.start()
    try:
        # Same CA, so the handshake succeeds: the refusal has to come from the credential, not from TLS.
        dev = await h.device(client_context("unlisted-client"))
        reply = await dev.register("tls-study-1")
        assert reply is not None and reply.type == REGISTER_REFUSED_EVENT, f"unexpected reply {reply}"
        assert "credential" in reply.data.get("reason", "")
        await asyncio.sleep(0.05)
        assert len(h.registry) == 0
    finally:
        await h.stop()


@check("7", "plaintext against a TLS listener registers nothing and gets no conversation")
async def t_plaintext_is_not_downgraded() -> None:
    fingerprint = fingerprint_of("listed-client")
    h = TlsHarness(credential_lookup={fingerprint: LISTED_DEVICE_ID}.get, ssl_context=server_context())
    await h.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", h.port)
        try:
            with contextlib.suppress(Exception):
                # A plaintext device claiming to be the real one. Nothing about this may work.
                writer.write(b'{"type":"describe"}\n')
                await writer.drain()
            try:
                event = await asyncio.wait_for(async_read_event(reader), TIMEOUT)
            except Exception:
                event = None
            assert event is None, "a plaintext peer must not be given a Wyoming conversation"
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        await asyncio.sleep(0.1)
        assert len(h.registry) == 0, "the listener must never register a plaintext device"
    finally:
        await h.stop()


@check("8", "the TLS paths are configurable and default beside the Gateway's other state")
async def t_config_paths() -> None:
    import os
    import subprocess

    code = (
        "import gateway.config as c;"
        "print(c.REGISTRY_TLS_CERT_FILE);print(c.REGISTRY_TLS_KEY_FILE);print(c.REGISTRY_TLS_CLIENT_CA_FILE)"
    )
    env = dict(os.environ)
    for name in ("VOICE_REGISTRY_TLS_CERT", "VOICE_REGISTRY_TLS_KEY", "VOICE_REGISTRY_TLS_CLIENT_CA"):
        env.pop(name, None)
    default = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(REPO), env=env,
                             capture_output=True, text=True, check=True).stdout.split()
    assert all("registry-tls" in line for line in default), default
    assert all(line.startswith(str(REPO / "state")) for line in default), default

    # The key is the one that matters: an operator may keep it root-owned rather than beside the state.
    env.update({
        "VOICE_REGISTRY_TLS_CERT": "/etc/gateway/server.crt",
        "VOICE_REGISTRY_TLS_KEY": "/etc/gateway/private/server.key",
        "VOICE_REGISTRY_TLS_CLIENT_CA": "/etc/gateway/client-ca.crt",
    })
    overridden = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(REPO), env=env,
                                capture_output=True, text=True, check=True).stdout.split()
    assert overridden == ["/etc/gateway/server.crt", "/etc/gateway/private/server.key",
                          "/etc/gateway/client-ca.crt"], overridden


@check("9", "the Gateway builds the listener's context from the configured paths, and fails closed")
async def t_bridge_wiring() -> None:
    # The behaviour of a context and of a listener is covered above; what this pins is the wiring, which
    # is the part a later change can silently drop while every check above still passes.
    source = (REPO / "Gateway" / "loop.py").read_text()
    assert "load_server_tls_context(" in source, "the Gateway must build the listener's context"
    for setting in ("REGISTRY_TLS_CERT_FILE", "REGISTRY_TLS_KEY_FILE", "REGISTRY_TLS_CLIENT_CA_FILE"):
        assert setting in source, f"the Gateway must read {setting}"
    assert "ssl_context=ssl_context" in source, "the listener must be given the context it was built"
    assert "TlsConfigError" in source, (
        "a half-configured certificate must be treated as a configuration error, not as a surprise"
    )


@check("10", "over TLS, a payload that claims another identity is still refused")
async def t_identity_mismatch_over_tls() -> None:
    fingerprint = fingerprint_of("listed-client")
    h = TlsHarness(credential_lookup={fingerprint: LISTED_DEVICE_ID}.get, ssl_context=server_context())
    await h.start()
    try:
        # The certificate says tls-kitchen-1 and always will; the payload does not get a vote.
        dev = await h.device(client_context("listed-client"))
        reply = await dev.register("tls-study-1")
        assert reply is not None and reply.type == REGISTER_REFUSED_EVENT, f"unexpected reply {reply}"
        assert "does not match" in reply.data.get("reason", "")
        await asyncio.sleep(0.05)
        assert len(h.registry) == 0
    finally:
        await h.stop()


@check("11", "the TLS and plain listeners share one registry, and a slot is one slot")
async def t_two_listeners_one_registry() -> None:
    fingerprint = fingerprint_of("listed-client")
    registry = SatelliteRegistry()
    tls_port, plain_port = free_port(), free_port()
    tls_server = RegistryServer(registry, "127.0.0.1", tls_port,
                                credential_lookup={fingerprint: LISTED_DEVICE_ID}.get,
                                ssl_context=server_context())
    # The migration listener believes self-claimed identities, which is what it is for; it is a
    # separate listener, not a weakening of the TLS one.
    plain_server = RegistryServer(registry, "127.0.0.1", plain_port, credential_lookup=None)
    await tls_server.start()
    await plain_server.start()
    try:
        tls_dev = TlsFakeDevice(tls_port, client_context("listed-client"))
        await tls_dev.connect()
        assert (await tls_dev.register(LISTED_DEVICE_ID)).type == REGISTER_OK_EVENT

        plain_dev = FakeDevice(plain_port)
        await plain_dev.connect()
        assert (await plain_dev.register("legacy-1")).type == REGISTER_OK_EVENT

        # One registry behind both: a device that came in either way is a device.
        assert sorted(registry.connected_devices()) == sorted([LISTED_DEVICE_ID, "legacy-1"])
        assert registry.get(LISTED_DEVICE_ID) is not None
        assert registry.get("legacy-1") is not None

        await tls_dev.close()
        await plain_dev.close()
        await asyncio.sleep(0.05)
        # Disconnect cleanup is registry-wide, not per listener.
        assert registry.connected_devices() == []
    finally:
        await tls_server.stop()
        await plain_server.stop()
        await registry.close_all()


@check("12", "the Gateway serves TLS on the TLS port and keeps plain text behind an explicit flag")
async def t_migration_wiring() -> None:
    source = (REPO / "Gateway" / "loop.py").read_text()
    assert "REGISTRY_TLS_PORT" in source and "REGISTRY_LEGACY_PLAINTEXT" in source
    # The load-bearing line: with a certificate present, the plain listener runs only when the
    # migration flag says so. Without a certificate it is the only listener, which is the local case.
    assert "plain_also_runs = ssl_context is None or REGISTRY_LEGACY_PLAINTEXT" in source, (
        "the plain listener must not run unconditionally once TLS exists"
    )
    assert "legacy_registry_server" in source, "the second listener needs its own handle"
    assert source.count("await server.stop()") == 1, "both listeners must be stopped on the same path"
    # And it must say so out loud when it does run, because that port takes self-claimed identities.
    assert "may claim any device identity" in source


# ---------------------------------------------------------------- runner

async def amain() -> int:
    failures = 0
    for number, description, fn in TESTS:
        try:
            await asyncio.wait_for(fn(), 30)
            print(f"  ok    {number:<4} {description}")
        except Exception as exc:  # noqa: BLE001 - a test failure is data, not a crash
            failures += 1
            print(f"  FAIL  {number:<4} {description}")
            print(f"        {type(exc).__name__}: {exc}")
    print(f"\nregistry TLS checks: {len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
