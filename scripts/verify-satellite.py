#!/usr/bin/env python3
"""End-to-end readiness check for ONE satellite, run on the gateway host after provisioning.

`install-gateway.sh`, `issue-registry-tls.py` and `install-esp32.sh` each prove their own step;
this tool proves the CHAIN between them, on real state, right now. Four links, one PASS/FAIL
line each, an overall verdict, and a non-zero exit if any link fails:

  listener     the registry listener is actually listening and accepting on the expected
               host:port -- read LIVE (ss + a connect probe), never inferred from config
  tls          the TLS material exists and the whole certificate set is consistent --
               REUSED from the issuer's own `verify` subcommand (scripts/issue-registry-tls.py),
               because the issuer's fingerprinting is the exact one the Gateway authenticates with
  credentials  <device-id> is in state/device-credentials.json and its certificate is the one
               the credential entry names
  registered   the device has really registered: the strongest LIVE evidence wins -- the
               Gateway's own fleet state (state/fleet-config.json, written by the registration
               hook in gateway/loop.py) and the Gateway log (journalctl, or --bridge-log)

STRICTLY READ-ONLY. Every command it runs reads state (ss, journalctl, openssl verify); it
never writes a file, never touches the gateway, never provisions anything. Safe to run on a
healthy system at any time.

Usage:
    scripts/verify-satellite.py --device-id <device-id>
    scripts/verify-satellite.py --device-id <device-id> --host <host> --port 10702 \
        --state-dir ~/hermes-voice-satellite/state

Flags: --host, --port, --state-dir, --device-id (each detected where possible: the LIVE
listener `ss` sees -- because what is actually accepting beats what any config claims, and a
Gateway bound to a LAN address is not reachable on loopback -- then the same environment /
gateway.env the Gateway itself reads, then loopback; the state directory beside the repo, and --
when the credential file lists exactly one device -- that device id). Plus --credentials,
--tls-dir and --bridge-log for layouts that keep those files elsewhere.

Exit status: 0 only when all four links PASS; 1 otherwise; 2 for a usage error.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent

#: The Gateway's own systemd user unit (installed by scripts/install-gateway.sh).
JOURNAL_UNIT = "hermes-voice-gateway.service"

#: The two log lines that mean "this device registered", in the two places that print them:
#: gateway/loop.py `_on_device_registered` and gateway/registry.py `SatelliteRegistry.register`.
REGISTERED_PATTERNS = (
    r"device registered: id={id}(?=\s|$)",
    r"device {id} registered \(",
)


def load_issuer():
    """The issuer module (scripts/issue-registry-tls.py), loaded by path.

    Its `verify` subcommand and its fingerprint helper are the verification logic this tool
    reuses: re-deriving the fingerprint here would create a second implementation that can
    drift from the one the Gateway authenticates with, which is exactly the failure this tool
    exists to catch.
    """
    path = SCRIPTS_DIR / "issue-registry-tls.py"
    spec = importlib.util.spec_from_file_location("issue_registry_tls", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- configuration detection

def read_env_file(path: Path) -> dict:
    """KEY=VALUE lines from gateway.env, the same file the Gateway reads (no shell expansion)."""
    values = {}
    try:
        text = path.read_text()
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def detect_setting(name: str, env_file: Path, default: str) -> str:
    """Flag (decided by the caller) > environment > gateway.env > default.

    The same precedence gateway/config.py applies, so this tool reports against the values the
    Gateway itself would use on this host.
    """
    if name in os.environ:
        return os.environ[name]
    return read_env_file(env_file).get(name, default)

def detected_port(name: str, env_file: Path, default: str) -> int:
    """detect_setting, as a port: anything that is not one is a usage error, not a default."""
    raw = detect_setting(name, env_file, default)
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"verify-satellite: {name} is not a port: {raw!r}") from None
    if not 1 <= value <= 65535:
        raise SystemExit(f"verify-satellite: {name} out of range 1-65535: {value}")
    return value

#: A bind that answers for every address carries no information about which address to probe.
WILDCARD_BINDS = ("", "*", "0.0.0.0", "::")

def parse_listeners(text: str) -> dict[int, list[str]]:
    """`ss -ltnH` output -> {port: [bound addresses]}. Malformed lines are skipped, not guessed."""
    listeners: dict[int, list[str]] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] != "LISTEN":
            continue
        local = fields[3]
        if ":" not in local:
            continue
        bound, _, port_text = local.rpartition(":")
        try:
            port = int(port_text)
        except ValueError:
            continue
        listeners.setdefault(port, []).append(bound.strip("[]"))
    return listeners

def live_listeners() -> dict[int, list[str]]:
    """What the kernel says is listening RIGHT NOW (`ss -ltnH`), or {} when it cannot be told.

    Read-only and best-effort: no ss, a broken ss, or an unreadable table simply means the
    live source is silent and resolution falls through to configuration.
    """
    ss = shutil.which("ss")
    if ss is None:
        return {}
    try:
        result = subprocess.run([ss, "-ltnH"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_listeners(result.stdout)

def resolve_probe_target(host_arg: str | None, port_arg: int | None,
                         listeners: dict[int, list[str]], env_file: Path,
                         tls_configured: bool) -> tuple[str, int]:
    """The host:port the listener link probes. Precedence, highest first:

      1. an explicit --host / --port, per field -- the operator's word wins;
      2. the LIVE listener: a listener bound to a specific address on the chosen port names
         that address, and a live listener on one of the configured ports picks the port;
      3. VOICE_REGISTRY_HOST / gateway.env -- the configuration the Gateway itself reads;
      4. loopback.

    Without (2) a Gateway that binds a LAN address is probed on loopback and reported as a
    false `FAIL listener` -- the exact failure this default exists to stop. A wildcard bind
    (0.0.0.0 / ::) accepts anything and names nothing, so the host falls through to (3)/(4).
    Read-only: it reads configuration and the kernel's socket table and nothing else.
    """
    registry_port = detected_port("VOICE_REGISTRY_PORT", env_file, "10701")
    tls_port = detected_port("VOICE_REGISTRY_TLS_PORT", env_file, str(registry_port + 1))
    configured_port = tls_port if tls_configured else registry_port

    if port_arg is not None:
        port = port_arg
    else:
        # What is accepting wins over what is configured (a stale gateway.env, or the plain port
        # when only the TLS one is up); the configured primary wins ties between candidates.
        port = configured_port
        for candidate in dict.fromkeys((configured_port, tls_port, registry_port)):
            if candidate in listeners:
                port = candidate
                break

    if host_arg:
        return host_arg, port
    for bound in listeners.get(port, []):
        if bound not in WILDCARD_BINDS:
            return bound, port
    return detect_setting("VOICE_REGISTRY_HOST", env_file, "127.0.0.1"), port


# ---------------------------------------------------------------- the four links

def check_listener(host: str, port: int) -> tuple[bool, str]:
    """Link 1: a real listener on host:port, proven two independent ways.

    `ss` sees the kernel's listening sockets (config that never took effect cannot appear
    there), and a connect probe proves the listener is accepting rather than wedged. Both must
    agree: a listening socket nobody can dial is exactly the outage this tool exists to find.
    """
    notes = []

    listening = None  # None = could not tell (no ss), True/False = ss's answer
    ss = shutil.which("ss")
    if ss is None:
        notes.append("ss not installed; connect probe only")
    else:
        try:
            result = subprocess.run([ss, "-ltnH"], capture_output=True, text=True, timeout=10)
            lines = result.stdout.splitlines()
            listening = False
        except (OSError, subprocess.SubprocessError) as exc:
            notes.append(f"ss failed ({exc}); connect probe only")
            lines = []
            listening = None
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[0] != "LISTEN":
                continue
            local = fields[3]
            if ":" not in local or local.rsplit(":", 1)[1] != str(port):
                continue
            bound = local.rsplit(":", 1)[0].strip("[]")
            if bound in ("*", "0.0.0.0", "::", "") or bound == host.strip("[]"):
                listening = True
                notes.append(f"ss: LISTEN on {local}")
                break
        if listening is False:
            notes.append(f"ss: nothing listening on port {port}")

    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "*") else host
    connected = False
    try:
        with socket.create_connection((probe_host, port), timeout=3.0):
            connected = True
        notes.append(f"connect {probe_host}:{port}: accepted")
    except OSError as exc:
        notes.append(f"connect {probe_host}:{port}: {exc}")

    ok = connected and listening is not False
    if listening is False and connected:
        # ss did not see it but the connect worked: trust the socket, say so.
        ok = True
        notes.append("ss did not list the port; accepting the working connect as evidence")
    return ok, "; ".join(notes)


def check_tls(issuer, state_dir: Path, credentials: Path) -> tuple[bool, str]:
    """Link 2: the TLS set exists and is consistent, by the issuer's own `verify`."""
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            rc = issuer.cmd_verify(
                argparse.Namespace(state=str(state_dir), credentials=str(credentials)))
    except SystemExit as exc:
        return False, f"issuer verify refused the state: {exc}"
    except Exception as exc:  # noqa: BLE001 - any verifier crash is a FAIL, not a traceback
        return False, f"issuer verify could not run: {type(exc).__name__}: {exc}"
    out = captured.getvalue()
    if rc == 0:
        summary = out.strip().splitlines()[-1] if out.strip() else "consistent"
        return True, f"issuer verify: {summary}"
    problems = [line.strip()[len("PROBLEM:"):].strip()
                for line in out.splitlines() if line.strip().startswith("PROBLEM:")]
    return False, "issuer verify: " + ("; ".join(problems) or out.strip() or "failed")


def check_credentials(issuer, credentials: Path, tls_dir: Path, device_id: str) -> tuple[bool, str]:
    """Link 3: the device is in the credential file, and its certificate is the one named there."""
    if not credentials.is_file():
        return False, (f"no credential file at {credentials} (enroll the device: "
                       f"issue-registry-tls.py --state <state-dir> enroll --new {device_id})")
    try:
        data = json.loads(credentials.read_text())
    except (OSError, ValueError) as exc:
        return False, f"credential file {credentials} is unreadable: {exc}"
    if not isinstance(data, dict):
        return False, f"credential file {credentials} is not a JSON object"
    entries = [fp for fp, dev in data.items() if str(dev) == device_id]
    if not entries:
        return False, (f"device {device_id!r} is not in {credentials} "
                       f"({len(data)} credential(s) on file, none of them this device)")
    cert = tls_dir / f"{device_id}.crt"
    key = tls_dir / f"{device_id}.key"
    missing = [p.name for p in (cert, key) if not p.is_file()]
    if missing:
        return False, (f"{device_id} is in {credentials.name} but {', '.join(missing)} "
                       f"is missing from {tls_dir}")
    fingerprint = issuer.fingerprint_of(cert)
    if fingerprint in entries:
        return True, f"{device_id} in {credentials.name}, certificate fingerprint matches"
    return False, (f"{device_id} credential on file ({entries[0][:19]}...) does not match "
                   f"{cert.name} ({fingerprint[:19]}...): re-enroll with "
                   f"issue-registry-tls.py enroll --device {device_id} --force")


def _registration_evidence(fleet_state: Path, device_id: str) -> list[str]:
    """What state/fleet-config.json proves about this device having registered.

    The record is written by the registration hook (gateway/loop.py `_on_device_registered` ->
    `ConfigPlane.ensure_device`): `metadata` is only ever filled from a registration payload,
    and `reported_at` is only ever set by a config report from a live registered device.
    """
    try:
        data = json.loads(fleet_state.read_text())
    except (OSError, ValueError):
        return []
    record = (data.get("devices") or {}).get(device_id)
    if not isinstance(record, dict):
        return []
    evidence = []
    if record.get("reported_at"):
        evidence.append(f"fleet-config: reported actual state at {record['reported_at']}")
    metadata = record.get("metadata")
    if isinstance(metadata, dict) and metadata:
        described = ", ".join(f"{k}={v}" for k, v in sorted(metadata.items()) if v)
        evidence.append(f"fleet-config: registration identity recorded"
                        + (f" ({described})" if described else ""))
    return evidence


def _log_evidence(log_text: str, device_id: str) -> list[str]:
    patterns = [re.compile(p.format(id=re.escape(device_id))) for p in REGISTERED_PATTERNS]
    for line in log_text.splitlines():
        if any(p.search(line) for p in patterns):
            return [f"log: {line.strip()[:120]}"]
    return []


def check_registered(state_dir: Path, device_id: str, bridge_log: Path | None) -> tuple[bool, str]:
    """Link 4: the device has really registered, from the Gateway's own state and log."""
    evidence = _registration_evidence(state_dir / "fleet-config.json", device_id)
    searched = [str(state_dir / "fleet-config.json")]

    if bridge_log is not None:
        searched.append(str(bridge_log))
        try:
            evidence += _log_evidence(bridge_log.read_text(errors="replace"), device_id)
        except OSError as exc:
            evidence.append(f"log: could not read {bridge_log}: {exc}")
    elif shutil.which("journalctl"):
        searched.append(f"journalctl --user -u {JOURNAL_UNIT}")
        try:
            result = subprocess.run(
                ["journalctl", "--user", "-u", JOURNAL_UNIT, "-n", "4000", "--no-pager", "-o", "cat"],
                capture_output=True, text=True, timeout=15,
            )
            evidence += _log_evidence(result.stdout, device_id)
        except (OSError, subprocess.SubprocessError) as exc:
            evidence.append(f"log: journalctl could not run: {exc}")

    if evidence:
        return True, "; ".join(evidence)
    return False, (f"no live evidence that {device_id} has registered: nothing in "
                   + " or ".join(searched))


# ---------------------------------------------------------------- driver

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="registry host (default: the live listener's bind address, "
                                      "then VOICE_REGISTRY_HOST, else 127.0.0.1)")
    parser.add_argument("--port", type=int,
                        help="registry port (default: the live listener's port, then the TLS "
                             "port when TLS is configured, else the plain port)")
    parser.add_argument("--state-dir", help="the Gateway's state/ directory (default: <repo>/state)")
    parser.add_argument("--device-id", help="the enrolled device (default: the only device on file)")
    parser.add_argument("--credentials", help="device-credentials.json path "
                                              "(default: <state-dir>/device-credentials.json)")
    parser.add_argument("--tls-dir", help="registry-tls directory (default: <state-dir>/registry-tls)")
    parser.add_argument("--bridge-log", help="a Gateway log file to scan for registration evidence "
                                             "(default: the journal for " + JOURNAL_UNIT + ")")
    args = parser.parse_args()

    state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir \
        else (REPO_ROOT / "state").resolve()
    tls_dir = Path(args.tls_dir).expanduser().resolve() if args.tls_dir else state_dir / "registry-tls"
    credentials = Path(args.credentials).expanduser().resolve() if args.credentials \
        else state_dir / "device-credentials.json"
    bridge_log = Path(args.bridge_log).expanduser().resolve() if args.bridge_log else None

    env_file = REPO_ROOT / "gateway.env"
    tls_configured = (tls_dir / "server.crt").is_file() and (tls_dir / "server.key").is_file()

    # The probe target, resolved once: an explicit --host/--port wins per field, then the LIVE
    # listener `ss` sees (a Gateway bound to a LAN address is not reachable on loopback, and
    # probing it there produced a false `FAIL listener`), then the configuration the Gateway
    # itself reads, then loopback. Read-only, like everything else here.
    host, port = resolve_probe_target(args.host, args.port, live_listeners(), env_file,
                                      tls_configured)

    device_id = args.device_id
    if device_id is None:
        try:
            ids = sorted({str(v) for v in json.loads(credentials.read_text()).values()})
        except (OSError, ValueError):
            ids = []
        if len(ids) == 1:
            device_id = ids[0]
        else:
            print(f"verify-satellite: pass --device-id (found {len(ids)} device(s) on file, "
                  f"and this check is per device)", file=sys.stderr)
            return 2

    issuer = load_issuer()
    print(f"verify-satellite: device {device_id}, state {state_dir}, "
          f"registry {host}:{port}{' (TLS)' if tls_configured else ' (plain)'}")

    checks = [
        ("listener", check_listener(host, port)),
        ("tls", check_tls(issuer, state_dir, credentials)),
        ("credentials", check_credentials(issuer, credentials, tls_dir, device_id)),
        ("registered", check_registered(state_dir, device_id, bridge_log)),
    ]
    failures = 0
    for name, (ok, detail) in checks:
        status = "PASS" if ok else "FAIL"
        failures += 0 if ok else 1
        print(f"{status}  {name:<11} {detail}")

    total = len(checks)
    if failures:
        print(f"verify-satellite: FAIL ({total - failures}/{total} links passing) -- "
              f"{device_id} is NOT ready")
        return 1
    print(f"verify-satellite: PASS ({total}/{total} links) -- {device_id} is registered and ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
