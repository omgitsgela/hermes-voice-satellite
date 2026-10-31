#!/usr/bin/env python3
"""Offline tests for scripts/verify-satellite.py, the post-provisioning readiness check.

Run with the Gateway's own interpreter:

    .venv/bin/python -B scripts/test_verify_satellite.py

Every scenario builds a THROWAWAY state directory with the REAL issuer
(scripts/issue-registry-tls.py init + enroll) and the REAL config plane
(gateway/config_plane.py), so the verifier is exercised against exactly the artifacts a
gateway host would hold. No live network is needed: the listener link is proven against a
loopback socket the test owns, and the registration evidence is written by the same code the
Gateway's registration hook calls.

Checks:

 1  a fully provisioned state passes all four links -- and the run changes NO state
 2  a port with nothing listening fails the listener link
 3  a missing credential file fails the credential link with the credential message
 4  a device key missing from the TLS dir fails and names the missing file
 5  a certificate the CA cannot verify fails the TLS link ("not verifiable")
 6  an unknown device id fails the credential link and names the device
 7  the default probe follows the LIVE listener (ss) before the configured host
 8  explicit --host/--port override the live listener
 9  with no live listener the probe defaults to the configured host, then loopback
10  a wildcard listener carries no host information and falls through to configuration
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = pathlib.Path(__file__).resolve().parent
VERIFY = SCRIPTS / "verify-satellite.py"
ISSUER = SCRIPTS / "issue-registry-tls.py"

sys.path.insert(0, str(REPO))

TESTS = []


def check(number: str, description: str):
    def wrap(fn):
        TESTS.append((number, description, fn))
        return fn
    return wrap


def load_issuer():
    """The issuer module, by path: its fingerprint_of is the credential computation under test."""
    spec = importlib.util.spec_from_file_location("issue_registry_tls_test", ISSUER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ISSUER_MOD = load_issuer()


def load_verify():
    """The verifier module, by path: its resolve_probe_target is the default-resolution
    order under test."""
    spec = importlib.util.spec_from_file_location("verify_satellite_test", VERIFY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# The throwaway identity. Nothing personal, nothing a deployment uses.
DEVICE_ID = "sat-test-1"


class StubListener:
    """A loopback TCP listener the verifier's connect probe can reach. The test owns it, so
    the listener link is proven against a real accepting socket with no external network. The
    bind address is a parameter (loopback is 127.0.0.0/8) so a test can hold a listener on a
    NON-default address and observe which address the verifier actually probes."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.sock = socket.create_server((host, 0))
        self.port = self.sock.getsockname()[1]
        self.running = True
        import threading

        def accept_loop():
            while self.running:
                try:
                    conn, _ = self.sock.accept()
                    conn.close()
                except OSError:
                    return

        self.thread = threading.Thread(target=accept_loop, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass


def free_port() -> int:
    """A port nothing is listening on: bind then release, so the connect probe is refused."""
    sock = socket.create_server(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def run_issuer(state: pathlib.Path, *argv: str) -> None:
    result = subprocess.run(
        [sys.executable, "-B", str(ISSUER), "--state", str(state), *argv],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"issuer {' '.join(argv)} failed:\n{result.stdout}\n{result.stderr}"


def build_state(root: pathlib.Path) -> pathlib.Path:
    """A complete, consistent gateway state: CA + server certificate + one enrolled device,
    minted by the real issuer, and a fleet record written by the real config plane."""
    state = root / "state"
    run_issuer(state, "init", "--san", "127.0.0.1")
    run_issuer(state, "enroll", "--new", DEVICE_ID)

    from satellite_gateway.config_plane import ConfigPlane

    plane = ConfigPlane(state / "fleet-config.json")
    plane.ensure_device(DEVICE_ID, metadata={
        "name": "field-test", "area": "test-bench", "firmware_version": "0.0-test",
        "boot_id": "boot-1", "capabilities": ["audio"],
    })
    return state


def write_bridge_log(root: pathlib.Path) -> pathlib.Path:
    """A Gateway log line in the exact shape satellite_gateway/loop.py's registration hook prints."""
    log = root / "satellite_gateway.log"
    log.write_text(
        f"INFO gateway.loop: device registered: id={DEVICE_ID} name=field-test area=test-bench "
        f"version=0.0-test protocol=1 capabilities=audio\n"
    )
    return log


def run_verifier(state: pathlib.Path, port: int, log: pathlib.Path,
                 *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(VERIFY),
         "--state-dir", str(state), "--host", "127.0.0.1", "--port", str(port),
         "--device-id", DEVICE_ID, "--bridge-log", str(log), *extra],
        capture_output=True, text=True, timeout=120,
    )

#: The environment variables that feed the verifier's configuration detection. Pinned
#: explicitly in every default-resolution run: they outrank satellite-gateway.env, so a developer's local
#: satellite-gateway.env or shell exports can never steer a test.
REGISTRY_ENV_NAMES = ("VOICE_REGISTRY_HOST", "VOICE_REGISTRY_PORT", "VOICE_REGISTRY_TLS_PORT")

def probe_env(**overrides: str) -> dict:
    """A child environment with the VOICE_REGISTRY_* detection inputs pinned by the caller."""
    env = dict(os.environ)
    env.update({"VOICE_REGISTRY_HOST": "127.0.0.1", "VOICE_REGISTRY_PORT": "10701",
                "VOICE_REGISTRY_TLS_PORT": "10702"})
    env.update(overrides)
    return env

@contextlib.contextmanager
def cleared_registry_env():
    """detect_setting reads os.environ first, so the in-process resolution tests pin the source
    by removing the VOICE_REGISTRY_* variables and restoring whatever was there afterwards."""
    saved = {name: os.environ.pop(name) for name in REGISTRY_ENV_NAMES if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def state_snapshot(state: pathlib.Path) -> dict:
    """Every file in the state dir, hashed: the read-only proof."""
    out = {}
    for path in sorted(state.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(state))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def write_rogue_device_cert(state: pathlib.Path, work: pathlib.Path) -> None:
    """Replace the device's certificate with one signed by a CA the Gateway does not trust,
    and fix the credential entry to the new fingerprint so the ONLY failure is the CA signature."""
    tls = state / "registry-tls"

    def openssl(*argv: str) -> None:
        result = subprocess.run(["openssl", *argv], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, f"openssl {' '.join(argv)} failed: {result.stderr}"

    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(work / "rogue.key"), "-out", str(work / "rogue.crt"),
            "-days", "3650", "-subj", "/CN=rogue-test-ca")
    openssl("req", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(tls / f"{DEVICE_ID}.key"), "-out", str(work / "rogue.csr"),
            "-subj", f"/CN={DEVICE_ID}")
    openssl("x509", "-req", "-in", str(work / "rogue.csr"),
            "-CA", str(work / "rogue.crt"), "-CAkey", str(work / "rogue.key"),
            "-CAcreateserial", "-out", str(tls / f"{DEVICE_ID}.crt"), "-days", "3650")

    credentials = state / "device-credentials.json"
    data = json.loads(credentials.read_text())
    data = {fp: dev for fp, dev in data.items() if dev != DEVICE_ID}
    data[ISSUER_MOD.fingerprint_of(tls / f"{DEVICE_ID}.crt")] = DEVICE_ID
    credentials.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


@check("1", "a fully provisioned state passes all four links, and the run changes no state")
def t_good_state() -> None:
    with tempfile.TemporaryDirectory(prefix="verify-satellite-good.") as tmp:
        root = pathlib.Path(tmp)
        state = build_state(root)
        log = write_bridge_log(root)
        listener = StubListener()
        try:
            before = state_snapshot(state)
            result = run_verifier(state, listener.port, log)
        finally:
            listener.close()
        assert result.returncode == 0, f"expected PASS, got rc={result.returncode}:\n{result.stdout}"
        out = result.stdout
        for link in ("listener", "tls", "credentials", "registered"):
            assert f"PASS  {link}" in out, f"{link} not a PASS line:\n{out}"
        assert "verify-satellite: PASS (4/4 links)" in out
        # Both registration evidence sources are reported (the fleet record is the durable one).
        assert "fleet-config" in out and "log:" in out, f"registration evidence missing:\n{out}"
        # Strictly read-only: not one byte of state may differ after a run.
        assert state_snapshot(state) == before, "the verifier modified the state directory"


@check("2", "a port with nothing listening fails the listener link")
def t_no_listener() -> None:
    with tempfile.TemporaryDirectory(prefix="verify-satellite-nolisten.") as tmp:
        root = pathlib.Path(tmp)
        state = build_state(root)
        log = write_bridge_log(root)
        result = run_verifier(state, free_port(), log)
        assert result.returncode == 1, f"expected rc=1, got {result.returncode}:\n{result.stdout}"
        assert "FAIL  listener" in result.stdout, result.stdout
        assert "verify-satellite: FAIL" in result.stdout


@check("3", "a missing credential file fails the credential link with the credential message")
def t_missing_credentials_file() -> None:
    with tempfile.TemporaryDirectory(prefix="verify-satellite-nocreds.") as tmp:
        root = pathlib.Path(tmp)
        state = build_state(root)
        log = write_bridge_log(root)
        (state / "device-credentials.json").unlink()
        listener = StubListener()
        try:
            result = run_verifier(state, listener.port, log)
        finally:
            listener.close()
        assert result.returncode == 1, result.stdout
        assert "FAIL  credentials" in result.stdout, result.stdout
        assert "no credential file at" in result.stdout, result.stdout


@check("4", "a device key missing from the TLS dir fails and names the missing file")
def t_missing_device_key() -> None:
    with tempfile.TemporaryDirectory(prefix="verify-satellite-nokey.") as tmp:
        root = pathlib.Path(tmp)
        state = build_state(root)
        log = write_bridge_log(root)
        (state / "registry-tls" / f"{DEVICE_ID}.key").unlink()
        listener = StubListener()
        try:
            result = run_verifier(state, listener.port, log)
        finally:
            listener.close()
        assert result.returncode == 1, result.stdout
        assert f"{DEVICE_ID}.key is missing" in result.stdout, result.stdout


@check("5", "a certificate the CA cannot verify fails the TLS link with 'not verifiable'")
def t_non_verifying_cert() -> None:
    with tempfile.TemporaryDirectory(prefix="verify-satellite-rogue.") as tmp:
        root = pathlib.Path(tmp)
        state = build_state(root)
        log = write_bridge_log(root)
        write_rogue_device_cert(state, root)
        listener = StubListener()
        try:
            result = run_verifier(state, listener.port, log)
        finally:
            listener.close()
        assert result.returncode == 1, result.stdout
        assert "FAIL  tls" in result.stdout, result.stdout
        assert "not verifiable" in result.stdout, result.stdout


@check("6", "an unknown device id fails the credential link and names the device")
def t_unknown_device() -> None:
    with tempfile.TemporaryDirectory(prefix="verify-satellite-unknown.") as tmp:
        root = pathlib.Path(tmp)
        state = build_state(root)
        log = write_bridge_log(root)
        listener = StubListener()
        try:
            result = subprocess.run(
                [sys.executable, "-B", str(VERIFY),
                 "--state-dir", str(state), "--host", "127.0.0.1", "--port", str(listener.port),
                 "--device-id", "ghost-9", "--bridge-log", str(log)],
                capture_output=True, text=True, timeout=120,
            )
        finally:
            listener.close()
        assert result.returncode == 1, result.stdout
        assert "FAIL  credentials" in result.stdout, result.stdout
        assert "ghost-9" in result.stdout and "is not in" in result.stdout, result.stdout


@check("7", "the default probe follows the LIVE listener (ss) before the configured host")
def t_default_probe_follows_the_live_listener() -> None:
    """The false-FAIL case, reproduced offline: the Gateway accepts on a NON-loopback address
    while the configured host says loopback. Probing 127.0.0.1 against a listener that is not
    there is what printed `FAIL listener` against a perfectly healthy Gateway -- the default
    must read what `ss` sees and probe THAT."""
    assert shutil.which("ss") is not None, "ss is required: the live listener is read from it"
    with tempfile.TemporaryDirectory(prefix="verify-satellite-live.") as tmp:
        root = pathlib.Path(tmp)
        state = build_state(root)
        log = write_bridge_log(root)
        listener = StubListener("127.0.0.2")  # loopback /8, but emphatically not 127.0.0.1
        try:
            result = subprocess.run(
                [sys.executable, "-B", str(VERIFY),
                 "--state-dir", str(state), "--device-id", DEVICE_ID, "--bridge-log", str(log)],
                capture_output=True, text=True, timeout=120,
                # The configuration says 127.0.0.1; only the live listener says 127.0.0.2. If
                # the resolver preferred configuration, the probe would hit 127.0.0.1 (where
                # nothing listens) and the listener link would FAIL.
                env=probe_env(VOICE_REGISTRY_HOST="127.0.0.1", VOICE_REGISTRY_PORT=str(listener.port),
                              VOICE_REGISTRY_TLS_PORT=str(listener.port)),
            )
        finally:
            listener.close()
        assert result.returncode == 0, f"expected PASS, got rc={result.returncode}:\n{result.stdout}"
        assert f"registry 127.0.0.2:{listener.port}" in result.stdout, result.stdout
        assert f"connect 127.0.0.2:{listener.port}: accepted" in result.stdout, result.stdout
        assert "PASS  listener" in result.stdout, result.stdout
        assert f"connect 127.0.0.1:{listener.port}" not in result.stdout, \
            "the probe must follow the live listener, not the configured host"


@check("8", "explicit --host/--port override the live listener")
def t_explicit_flags_override_the_live_listener() -> None:
    """The operator's word wins: --host/--port are honored even when the live listener (and the
    configuration) point somewhere else, per field."""
    with tempfile.TemporaryDirectory(prefix="verify-satellite-flags.") as tmp:
        root = pathlib.Path(tmp)
        state = build_state(root)
        log = write_bridge_log(root)
        live = StubListener("127.0.0.2")     # what the live listener and the config would pick
        chosen = StubListener("127.0.0.1")   # what the flags name
        try:
            result = subprocess.run(
                [sys.executable, "-B", str(VERIFY),
                 "--state-dir", str(state), "--host", "127.0.0.1", "--port", str(chosen.port),
                 "--device-id", DEVICE_ID, "--bridge-log", str(log)],
                capture_output=True, text=True, timeout=120,
                env=probe_env(VOICE_REGISTRY_HOST="127.0.0.2", VOICE_REGISTRY_PORT=str(live.port),
                              VOICE_REGISTRY_TLS_PORT=str(live.port)),
            )
        finally:
            live.close()
            chosen.close()
        assert result.returncode == 0, f"expected PASS, got rc={result.returncode}:\n{result.stdout}"
        assert f"registry 127.0.0.1:{chosen.port}" in result.stdout, result.stdout
        assert f"connect 127.0.0.1:{chosen.port}: accepted" in result.stdout, result.stdout
        assert "127.0.0.2" not in result.stdout, \
            "the overridden live listener must not be probed or reported"


@check("9", "with no live listener the probe defaults to the configured host, then loopback")
def t_probe_default_falls_back_to_configuration_then_loopback() -> None:
    """The rest of the order: a silent live source falls through to VOICE_REGISTRY_HOST /
    satellite-gateway.env, and with nothing configured either, to loopback. And when the live source DOES
    speak, it wins over the configured host and even picks the port when configuration's is
    dark (a stale satellite-gateway.env)."""
    verify = load_verify()
    with tempfile.TemporaryDirectory(prefix="verify-satellite-resolve.") as tmp:
        env_file = pathlib.Path(tmp) / "satellite-gateway.env"
        env_file.write_text("VOICE_REGISTRY_HOST=192.0.2.10\nVOICE_REGISTRY_PORT=10701\n"
                            "VOICE_REGISTRY_TLS_PORT=10702\n")
        with cleared_registry_env():
            assert verify.resolve_probe_target(None, None, {}, env_file, True) == ("192.0.2.10", 10702)
            assert verify.resolve_probe_target(None, None, {}, env_file, False) == ("192.0.2.10", 10701)
            live = verify.parse_listeners("LISTEN 0 128 198.51.100.7:10702 0.0.0.0:*\n")
            assert verify.resolve_probe_target(None, None, live, env_file, True) == ("198.51.100.7", 10702)
            # A live listener on the OTHER configured port picks the port too: what is
            # accepting beats what satellite-gateway.env claims.
            assert verify.resolve_probe_target(None, None, live, env_file, False) == ("198.51.100.7", 10702)
        env_file.write_text("")
        with cleared_registry_env():
            assert verify.resolve_probe_target(None, None, {}, env_file, True) == ("127.0.0.1", 10702)
            assert verify.resolve_probe_target(None, None, {}, env_file, False) == ("127.0.0.1", 10701)


@check("10", "a wildcard listener carries no host information and falls through to configuration")
def t_wildcard_listener_falls_through_to_configuration() -> None:
    """0.0.0.0 / :: accept everything and name nothing: the host must come from configuration
    (or loopback), while the live port still picks the port. Explicit flags still win above all."""
    verify = load_verify()
    with tempfile.TemporaryDirectory(prefix="verify-satellite-wildcard.") as tmp:
        env_file = pathlib.Path(tmp) / "satellite-gateway.env"
        env_file.write_text("VOICE_REGISTRY_HOST=192.0.2.10\nVOICE_REGISTRY_PORT=10701\n"
                            "VOICE_REGISTRY_TLS_PORT=10702\n")
        with cleared_registry_env():
            live = verify.parse_listeners(
                "LISTEN 0 128 0.0.0.0:10702 0.0.0.0:*\nLISTEN 0 128 [::]:10702 [::]:*\n")
            assert verify.resolve_probe_target(None, None, live, env_file, True) == ("192.0.2.10", 10702)
            assert verify.resolve_probe_target("198.51.100.7", 10701, live, env_file, True) \
                == ("198.51.100.7", 10701)


# ---------------------------------------------------------------- runner

def main() -> int:
    if shutil.which("openssl") is None:
        print("openssl is required (the issuer mints certificates with it); cannot run")
        return 1
    failures = 0
    for number, description, fn in TESTS:
        try:
            fn()
            print(f"  ok    {number:<4} {description}")
        except Exception as exc:  # noqa: BLE001 - a test failure is data, not a crash
            failures += 1
            print(f"  FAIL  {number:<4} {description}")
            print(f"        {type(exc).__name__}: {exc}")
    print(f"\nverify-satellite checks: {len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
