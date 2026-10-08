#!/usr/bin/env python3
"""Offline tests for scripts/verify-satellite.py, the post-provisioning readiness check.

Run with the bridge's own interpreter:

    .venv/bin/python -B scripts/test_verify_satellite.py

Every scenario builds a THROWAWAY state directory with the REAL issuer
(scripts/issue-registry-tls.py init + enroll) and the REAL config plane
(bridge/config_plane.py), so the verifier is exercised against exactly the artifacts a
gateway host would hold. No live network is needed: the listener link is proven against a
loopback socket the test owns, and the registration evidence is written by the same code the
bridge's registration hook calls.

Checks:

 1  a fully provisioned state passes all four links -- and the run changes NO state
 2  a port with nothing listening fails the listener link
 3  a missing credential file fails the credential link with the credential message
 4  a device key missing from the TLS dir fails and names the missing file
 5  a certificate the CA cannot verify fails the TLS link ("not verifiable")
 6  an unknown device id fails the credential link and names the device
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
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

# The throwaway identity. Nothing personal, nothing a deployment uses.
DEVICE_ID = "sat-test-1"


class StubListener:
    """A loopback TCP listener the verifier's connect probe can reach. The test owns it, so
    the listener link is proven against a real accepting socket with no external network."""

    def __init__(self) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
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

    from bridge.config_plane import ConfigPlane

    plane = ConfigPlane(state / "fleet-config.json")
    plane.ensure_device(DEVICE_ID, metadata={
        "name": "field-test", "area": "test-bench", "firmware_version": "0.0-test",
        "boot_id": "boot-1", "capabilities": ["audio"],
    })
    return state


def write_bridge_log(root: pathlib.Path) -> pathlib.Path:
    """A bridge log line in the exact shape bridge/loop.py's registration hook prints."""
    log = root / "bridge.log"
    log.write_text(
        f"INFO bridge.loop: device registered: id={DEVICE_ID} name=field-test area=test-bench "
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


def state_snapshot(state: pathlib.Path) -> dict:
    """Every file in the state dir, hashed: the read-only proof."""
    out = {}
    for path in sorted(state.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(state))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def write_rogue_device_cert(state: pathlib.Path, work: pathlib.Path) -> None:
    """Replace the device's certificate with one signed by a CA the bridge does not trust,
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
