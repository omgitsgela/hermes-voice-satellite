#!/usr/bin/env python3
"""Mint the bridge's registry TLS material and enroll satellite devices into it.

This is the host-side half of the device story. A satellite speaks to the registry over mutual
TLS: the bridge presents `server.crt` (signed by the client CA), each device presents a
certificate the same CA signed, and the bridge authenticates the device by the SHA-256 of its
certificate's DER form -- the fingerprint stored in `device-credentials.json`.

Nothing here connects to a device. It produces the files a device install then pushes over USB:

  state/registry-tls/client-ca.crt    the CA that signs device certificates (shared with devices)
  state/registry-tls/server.crt       the bridge's own certificate (signed by the client CA)
  state/registry-tls/server.key       its private key (0600)
  state/registry-tls/<device>.crt     one certificate per enrolled device (signed by the CA)
  state/registry-tls/<device>.key     its private key (0600)
  state/device-credentials.json       fingerprint -> device id, 0600

`init` makes the state usable by the bridge: with no CA it creates one and the server
certificate; with an existing CA it reuses it (a CA key already on disk is never regenerated
without --force, because every enrolled device would stop authenticating). `enroll` signs a
device certificate, registers its fingerprint, and prints the exact `install-esp32.sh` /
`install-pi.sh` command that provisions it. `verify` re-checks the whole set, offline.

The fingerprint is computed exactly as the bridge computes it (scripts/provision-device-tls.py's
pem_to_der -> sha256 of the DER), so an enrolled device is accepted by the very lookup the
listener builds. OpenSSL is the only external tool; it is a host tool, and a host that cannot
open a terminal to openssl is not a host this installer targets.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import ssl
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# The fingerprint must be computed the same way the provisioner computes it, so the credential
# file this writes and the region that script writes into a device always agree. One producer.
_spec = importlib.util.spec_from_file_location(
    "provision_device_tls", REPO_ROOT / "scripts" / "provision-device-tls.py")
assert _spec is not None and _spec.loader is not None, "cannot load provision-device-tls.py"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
pem_to_der = _mod.pem_to_der

DEFAULT_DAYS = 3650
CN_PREFIX = "hermes-voice-bridge"


def run_openssl(args: list[str], *extra: list[str]) -> str:
    """Run openssl; a failure here is a broken host toolchain, not something to work around."""
    try:
        result = subprocess.run(["openssl", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit("openssl not found on this host. Install it (apt install openssl / "
                         "dnf install openssl) and re-run.")
    if result.returncode != 0:
        raise SystemExit(f"openssl {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def fingerprint_of(cert_path: Path) -> str:
    """sha256:<hex> of the certificate's DER form -- the credential the bridge looks up."""
    return "sha256:" + hashlib.sha256(pem_to_der(cert_path.read_bytes())).hexdigest()


def check_key_matches(cert_path: Path, key_path: Path) -> None:
    """A certificate whose key does not match is a listener that handshakes then fails, or a
    device that will never authenticate. Refuse it at issue time, not in the field."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise SystemExit(f"{cert_path.name} / {key_path.name} do not form a usable pair: {exc}")


def check_ca_signature(ca_crt: Path, cert: Path, problem) -> None:
    """A device certificate the CA cannot verify will never complete the handshake. We use
    `openssl verify` because it checks exactly what the bridge's listener will check (the chain
    up to this CA) and keeps this tool free of the `cryptography` package, which the project
    deliberately does not depend on."""
    result = subprocess.run(["openssl", "verify", "-CAfile", str(ca_crt), str(cert)],
                            capture_output=True, text=True)
    if result.returncode != 0 or result.stdout.rstrip().endswith("OK") is False:
        problem(f"{cert.name} is not verifiable against {ca_crt.name}: "
                f"{(result.stdout or result.stderr).strip()}")


def load_credentials(cfile: Path) -> dict:
    if cfile.is_file():
        data = json.loads(cfile.read_text())
        if not isinstance(data, dict):
            raise SystemExit(f"{cfile} is not a JSON object; refusing to append to it")
        return data
    return {}


def save_credentials(cfile: Path, data: dict) -> None:
    cfile.parent.mkdir(parents=True, exist_ok=True)
    cfile.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(cfile, 0o600)


def cmd_init(args: argparse.Namespace) -> int:
    state = Path(args.state).expanduser().resolve()
    tls = state / "registry-tls"
    tls.mkdir(parents=True, exist_ok=True)
    ca_crt, ca_key = tls / "ca.crt", tls / "ca.key"
    client_ca = tls / "client-ca.crt"
    server_crt, server_key = tls / "server.crt", tls / "server.key"
    days = args.days

    if ca_crt.is_file() or ca_key.is_file():
        if not (ca_crt.is_file() and ca_key.is_file()):
            raise SystemExit(f"partial CA state at {tls} (ca.crt and ca.key must both exist)")
        if args.force:
            print(f"  --force: regenerating the CA (existing {ca_crt.name} is replaced; every "
                  f"enrolled device will need re-enrolling)")
            ca_crt.unlink()
            ca_key.unlink()
        else:
            print(f"  CA already exists at {ca_crt.name}; reusing it")
    if not ca_key.is_file():
        run_openssl(["req", "-x509", "-newkey", "rsa:2048", "-nodes",
                     "-keyout", str(ca_key), "-out", str(ca_crt),
                     "-days", str(days), "-subj", f"/CN={CN_PREFIX}-client-ca"])
        os.chmod(ca_key, 0o600)

    # The bridge verifies the client CA by file; client-ca.crt is the file it loads. It is the
    # same key as ca.crt here because one CA signs both the server and the devices -- keeping the
    # two names explicit means the layout matches what the firmware region stores.
    client_ca.write_bytes(ca_crt.read_bytes())

    if server_crt.is_file() or server_key.is_file():
        if not (server_crt.is_file() and server_key.is_file()):
            raise SystemExit(f"partial server state at {tls}; remove both or pass --force")
        if args.force:
            print("  --force: reissuing the server certificate")
            server_crt.unlink()
            server_key.unlink()
        else:
            print(f"  server certificate already exists at {server_crt.name}; reusing it")
            print("  (devices pin its fingerprint -- reissuing it without re-provisioning the "
                  "devices would strand them)")
    if not server_key.is_file():
        san_entries = []
        for h in (args.san or "localhost,127.0.0.1").split(","):
            h = h.strip()
            if not h:
                continue
            # openssl wants explicit types: a bare address is IP:..., anything else DNS:...
            san_entries.append((f"IP:{h}" if h.replace(".", "").isdigit() else f"DNS:{h}"))
        if not san_entries:
            san_entries = ["DNS:localhost", "IP:127.0.0.1"]
        san = ",".join(san_entries)
        csr = tls / "server.csr"
        run_openssl(["req", "-newkey", "rsa:2048", "-nodes",
                     "-keyout", str(server_key), "-out", str(csr), "-subj", "/CN=localhost"])
        ext = tls / "server.ext"
        ext.write_text(f"subjectAltName={san}\nextendedKeyUsage=serverAuth\n")
        run_openssl(["x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
                     "-CAcreateserial", "-out", str(server_crt), "-days", str(days),
                     "-extfile", str(ext)])
        csr.unlink()
        ext.unlink()
        os.chmod(server_key, 0o600)
        print(f"  issued {server_crt.name} (SAN: {san})")

    os.chmod(tls, 0o700)
    fp = fingerprint_of(server_crt)
    print(f"\ninit complete: {tls}")
    print(f"  server certificate fingerprint (what devices pin): {fp}")
    print("  next: enroll each satellite with: issue-registry-tls.py --state <state> enroll "
          "--new <device-id> --endpoint-host <this-host> --endpoint-port <tls-port>")
    return 0


def cmd_enroll(args: argparse.Namespace) -> int:
    state = Path(args.state).expanduser().resolve()
    tls = state / "registry-tls"
    ca_crt, ca_key = tls / "ca.crt", tls / "ca.key"
    cfile = Path(args.credentials).expanduser().resolve() if args.credentials else state / "device-credentials.json"
    days = args.days

    if not ca_crt.is_file() or not ca_key.is_file():
        raise SystemExit(f"no CA at {tls}; run init first")
    device = args.new or args.device
    if not device:
        raise SystemExit("enroll needs --new <device-id> (a fresh device) or --device <id> (reissue)")
    if not device.isascii() or not all(c.isalnum() or c in "-_" for c in device):
        raise SystemExit(f"device id {device!r} may contain only letters, digits, '-' and '_'")
    dev_crt, dev_key = tls / f"{device}.crt", tls / f"{device}.key"

    if dev_crt.is_file() or dev_key.is_file():
        if not args.force:
            raise SystemExit(f"{device}.crt already exists at {tls}; pass --force to reissue "
                             f"(the old certificate's credential entry is replaced)")
        if dev_crt.is_file():
            old_fp = fingerprint_of(dev_crt)
            dev_crt.unlink()
        if dev_key.is_file():
            dev_key.unlink()

    csr = tls / f"{device}.csr"
    run_openssl(["req", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(dev_key), "-out", str(csr), "-subj", f"/CN={device}"])
    ext = tls / f"{device}.ext"
    ext.write_text("basicConstraints=CA:FALSE\nextendedKeyUsage=clientAuth\n")
    run_openssl(["x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
                 "-CAcreateserial", "-out", str(dev_crt), "-days", str(days), "-extfile", str(ext)])
    csr.unlink()
    ext.unlink()
    os.chmod(dev_key, 0o600)
    check_key_matches(dev_crt, dev_key)

    fp = fingerprint_of(dev_crt)
    creds = load_credentials(cfile)
    for other_fp, other_id in creds.items():
        if other_id == device and other_fp != fp:
            print(f"  note: replacing the old credential for {device} ({other_fp[:12]}...)")
    creds[fp] = device
    save_credentials(cfile, creds)

    os.chmod(tls, 0o700)
    print(f"\n{device} enrolled ({len(creds)} device(s) on file)")
    print(f"  fingerprint : {fp}")
    print(f"  certificate : {dev_crt}")
    print(f"  credentials : {cfile}")
    if args.endpoint_host and args.endpoint_port:
        endpoint = f"tls://{args.endpoint_host}:{args.endpoint_port}"
        print("\nprovision it with:")
        print(f"  scripts/install-esp32.sh --provision --port <usb-port> \\")
        print(f"      --tls-certs {tls} --device-id {device} \\")
        print(f"      --endpoint {endpoint} --wifi-ssid <ssid> --wifi-psk-file <file>")
        print(f"  sudo scripts/install-pi.sh --base <install-dir> --device-name {device} \\")
        print(f"      --endpoint {endpoint} --tls-certs {tls}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    state = Path(args.state).expanduser().resolve()
    tls = state / "registry-tls"
    ca_crt, ca_key = tls / "ca.crt", tls / "ca.key"
    cfile = Path(args.credentials).expanduser().resolve() if args.credentials else state / "device-credentials.json"
    problems = []

    def problem(msg: str) -> None:
        problems.append(msg)
        print(f"  PROBLEM: {msg}")

    if not (ca_crt.is_file() and ca_key.is_file()):
        problem(f"no CA pair at {tls}")
        return 1
    if not (tls / "client-ca.crt").is_file():
        problem("client-ca.crt missing (the bridge loads it to authenticate devices)")
    else:
        if (tls / "client-ca.crt").read_bytes() != ca_crt.read_bytes():
            problem("client-ca.crt differs from ca.crt")
    for name in ("server.crt", "server.key"):
        if not (tls / name).is_file():
            problem(f"{name} missing (the bridge cannot start the TLS listener without it)")
    check_key_matches(tls / "server.crt", tls / "server.key")

    creds = load_credentials(cfile)
    if not creds:
        problem(f"{cfile} has no entries; no device can authenticate")
    for fp, device in creds.items():
        if not fp.startswith("sha256:"):
            problem(f"credential key {fp[:12]}... is not a sha256 fingerprint")
        dev_crt, dev_key = tls / f"{device}.crt", tls / f"{device}.key"
        if not dev_crt.is_file() or not dev_key.is_file():
            problem(f"{device}: listed in the credentials but {dev_crt.name}/{dev_key.name} is missing")
            continue
        if fingerprint_of(dev_crt) != fp:
            problem(f"{device}: certificate fingerprint does not match the credential on file "
                    f"(the bridge will refuse it)")
        check_key_matches(dev_crt, dev_key)
        # The device's certificate must be signed by this CA: a certificate the CA cannot
        # verify is a device that will never complete the handshake, whatever the credentials say.
        check_ca_signature(ca_crt, dev_crt, problem)
    for entry in sorted(tls.iterdir()):
        if entry.name.endswith(".crt") and entry.stem not in creds.values() \
                and entry.name not in ("ca.crt", "client-ca.crt", "server.crt"):
            problem(f"{entry.name} exists but is not listed in {cfile.name} "
                    f"(a device that authenticates to nothing)")

    if problems:
        print(f"\nverify: {len(problems)} problem(s)")
        return 1
    print(f"verify: {len(creds)} enrolled device(s), CA + server certificate + credentials all consistent")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", default=".", help="the bridge's state/ directory (default: current)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create or reuse the CA + server certificate")
    p_init.add_argument("--san", help="comma-separated SAN hostnames for the server certificate "
                                      "(default: localhost,127.0.0.1)")
    p_init.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"certificate validity in days (default {DEFAULT_DAYS})")
    p_init.add_argument("--force", action="store_true", help="regenerate the CA / reissue the server certificate")
    p_init.set_defaults(func=cmd_init)

    p_enroll = sub.add_parser("enroll", help="sign a device certificate and register its fingerprint")
    group = p_enroll.add_mutually_exclusive_group()
    group.add_argument("--new", help="enroll a NEW device under this id")
    group.add_argument("--device", help="reissue an EXISTING device's certificate (requires --force)")
    p_enroll.add_argument("--credentials", help="the device-credentials.json path (default: <state>/device-credentials.json)")
    p_enroll.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p_enroll.add_argument("--force", action="store_true", help="replace an existing device's certificate")
    p_enroll.add_argument("--endpoint-host", help="print the provisioning command for this host")
    p_enroll.add_argument("--endpoint-port", type=int, help="with --endpoint-host")
    p_enroll.set_defaults(func=cmd_enroll)

    p_verify = sub.add_parser("verify", help="re-check CA, server certificate, devices and credentials")
    p_verify.add_argument("--credentials")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
