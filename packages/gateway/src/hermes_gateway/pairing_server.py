"""The gateway's side of pairing: prove the words, mint a device identity, hand it back.

This is the piece that makes a two-machine install work. The gateway holds the certificate authority
and the enrolment words; a workbench on some other machine has only the words and the address. Before
this endpoint existed, a device certificate could only be minted where the CA lived, so a gateway on a
server forced somebody to carry four files by hand.

The contract:

    POST /pair   {"words": "five words", "device": "esp32-0011223344"}
      -> 200 {"ok": true, "ca_pem": ..., "cert_pem": ..., "key_pem": ...,
              "server_pin": "<sha256 hex>", "dial_address": "tls://host:10702"}
      -> 401 {"ok": false, "error": "the pairing words are not correct"}
      -> 429 {"ok": false, "error": "too many attempts, try again later"}
      -> 400 {"ok": false, "error": "..."}

Three rules that matter more than the code:

- **The words are compared in constant time** and never logged, never echoed, and never included in
  an error.
- **Failures are rate limited per source.** The words are the only thing standing between the
  internet and the certificate authority, so an unlimited guess rate would make forty bits of
  entropy meaningless.
- **A device id is validated before it reaches the filesystem**, because it becomes a filename.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Protocol

DEVICE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

#: Names Windows refuses to create, whatever the extension: CON, PRN, AUX, NUL, COM1-9, LPT1-9.
RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {
    f"{stem}{digit}" for stem in ("com", "lpt") for digit in range(1, 10)
}


def valid_device_id(value: str) -> bool:
    """A device id becomes a filename, so it is validated where it is used, not where it arrives."""
    return bool(DEVICE_ID.fullmatch(value)) and value not in RESERVED_NAMES

MAX_BODY_BYTES = 4096


class PairingRefused(Exception):
    """Refused for a reason worth telling the caller, without giving an oracle to an attacker."""


@dataclass(frozen=True)
class PairingConfig:
    state_dir: Path
    dial_address: str
    enroll_script: Path | None = None
    max_failures: int = 5
    lockout_seconds: float = 300.0
    #: How long a source must wait between attempts even before it is locked out.
    min_interval_seconds: float = 0.5

    def token_path(self) -> Path:
        return self.state_dir / "enroll-token.txt"

    def tls_dir(self) -> Path:
        return self.state_dir / "registry-tls"


class Enroller(Protocol):
    """Signs a device certificate and returns where the certificate and key landed."""

    def enroll(self, device_id: str) -> tuple[Path, Path]: ...


class ScriptEnroller:
    """Runs the existing `issue-registry-tls.py enroll`, which already knows how to sign."""

    def __init__(self, script: Path, state_dir: Path) -> None:
        self.script = Path(script)
        self.state_dir = Path(state_dir)

    def enroll(self, device_id: str) -> tuple[Path, Path]:
        command = [
            "python3", str(self.script),
            "--state", str(self.state_dir),
            "enroll", "--new", device_id,
        ]
        finished = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if finished.returncode != 0:
            raise PairingRefused(
                f"the gateway could not issue a certificate for {device_id}: "
                f"{(finished.stderr or finished.stdout or '').strip()[:300]}"
            )
        tls = self.state_dir / "registry-tls"
        cert, key = tls / f"{device_id}.crt", tls / f"{device_id}.key"
        if not (cert.exists() and key.exists()):
            raise PairingRefused("the gateway did not produce a certificate for this device")
        return cert, key


@dataclass
class _SourceState:
    failures: int = 0
    locked_until: float = 0.0
    last_attempt: float = 0.0


class PairingService:
    """The whole endpoint, minus HTTP. Testable with a stub enroller and a temp directory."""

    def __init__(
        self,
        config: PairingConfig,
        *,
        enroller: Enroller | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.clock = clock
        self.enroller = enroller or ScriptEnroller(
            config.enroll_script or Path("scripts") / "issue-registry-tls.py", config.state_dir
        )
        self._sources: dict[str, _SourceState] = {}
        # Re-entrant because pair() holds it across the whole attempt, including the rate helpers.
        self._lock = threading.RLock()

    # -- the words ----------------------------------------------------------------------------

    def _expected_words(self) -> str:
        try:
            stored = self.config.token_path().read_text().strip()
        except OSError as exc:
            raise PairingRefused(
                "this gateway has no pairing words yet, so it cannot enrol anything"
            ) from exc
        if not stored:
            raise PairingRefused("this gateway's pairing words are empty")
        return stored

    def _words_match(self, supplied: str) -> bool:
        # Normalising whitespace and case only, never punctuation inside the words: the stored value
        # is what the person was given, and this is the one place that decides whether they match.
        normalise = lambda text: " ".join(text.split()).lower()  # noqa: E731
        expected = normalise(self._expected_words())
        return hmac.compare_digest(normalise(supplied), expected)

    # -- rate limiting ------------------------------------------------------------------------

    #: Beyond this many tracked sources, idle entries are dropped. A public endpoint sees many
    #: addresses and must not turn that into unbounded memory.
    MAX_TRACKED_SOURCES = 4096

    def _source_state(self, source: str) -> _SourceState:
        with self._lock:
            if source not in self._sources and len(self._sources) >= self.MAX_TRACKED_SOURCES:
                now = self.clock()
                for key, state in list(self._sources.items()):
                    if state.locked_until <= now and now - state.last_attempt > self.config.lockout_seconds:
                        del self._sources[key]
            return self._sources.setdefault(source, _SourceState())

    def _check_rate(self, source: str) -> _SourceState:
        state = self._source_state(source)
        now = self.clock()
        if state.locked_until > now:
            raise PairingRefused("too many attempts; wait a few minutes and try again")
        if now - state.last_attempt < self.config.min_interval_seconds:
            raise PairingRefused("too many attempts; wait a moment and try again")
        state.last_attempt = now
        return state

    def _record_failure(self, source: str) -> None:
        state = self._source_state(source)
        state.failures += 1
        if state.failures >= self.config.max_failures:
            state.locked_until = self.clock() + self.config.lockout_seconds
            state.failures = 0

    def _record_success(self, source: str) -> None:
        state = self._source_state(source)
        state.failures = 0
        state.locked_until = 0.0

    # -- pairing ------------------------------------------------------------------------------

    def pair(self, words: str, device_id: str, source: str = "unknown") -> dict:
        """Return the payload a workbench needs, or raise PairingRefused with a safe message."""
        if not isinstance(words, str) or not words.strip():
            raise PairingRefused("the pairing words are missing")
        if not isinstance(device_id, str) or not valid_device_id(device_id):
            # The id becomes a filename, so it is checked before it can reach the filesystem.
            raise PairingRefused(
                "a device name is required: lowercase letters, digits, hyphen or underscore"
            )

        # One lock around the whole attempt: the rate decision and the record must not interleave
        # with another attempt, and two enrolments must not write the shared credential registry at
        # the same time (the second would silently drop the first device).
        with self._lock:
            self._check_rate(source)
            if not self._words_match(words):
                self._record_failure(source)
                # Deliberately identical to a missing token: nothing tells a guesser how close
                # they are.
                raise PairingRefused("the pairing words are not correct")
            cert, key = self.enroller.enroll(device_id)
            # Recorded inside the same lock: a success must clear the counter before the next
            # attempt is judged.
            self._record_success(source)
        tls = self.config.tls_dir()
        payload = {
            "ok": True,
            "ca_pem": (tls / "client-ca.crt").read_text(),
            "cert_pem": Path(cert).read_text(),
            "key_pem": Path(key).read_text(),
            "server_pin": self.server_pin_hex(),
            # Public material, and required by the provisioning step: it recomputes the pin from the
            # certificate rather than trusting a number.
            "server_cert_pem": (tls / "server.crt").read_text(),
            "dial_address": self.config.dial_address,
        }
        return payload

    def server_pin_hex(self) -> str:
        """The fingerprint a device pins: SHA-256 of the server certificate's DER form."""
        server_crt = self.config.tls_dir() / "server.crt"
        raw = server_crt.read_bytes()
        der = _pem_to_der(raw) if b"-----BEGIN CERTIFICATE-----" in raw else raw
        return hashlib.sha256(der).hexdigest()


def _pem_to_der(pem: bytes) -> bytes:
    import base64

    body = b"".join(
        line.strip() for line in pem.splitlines() if line and not line.startswith(b"-----")
    )
    return base64.b64decode(body)


# -- HTTP -------------------------------------------------------------------------------------


class PairingHandler(BaseHTTPRequestHandler):
    service: PairingService

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        # Nothing from this endpoint is written to a request log: a body can contain the words.
        pass

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        # Pairing is a POST with a body. A GET is answered plainly so a browser hitting this port by
        # accident learns nothing and causes nothing.
        self._send(405, {"ok": False, "error": "pairing accepts POST only"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/") != "/pair":
            self._send(404, {"ok": False, "error": "not found"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._send(415, {"ok": False, "error": "expected application/json"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(400, {"ok": False, "error": "a small JSON body is required"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "the body is not JSON"})
            return

        source = self.client_address[0] if self.client_address else "unknown"
        try:
            payload = self.service.pair(
                str(body.get("words", "")), str(body.get("device", "")), source
            )
        except PairingRefused as exc:
            message = str(exc)
            status = 429 if "too many attempts" in message else (
                401 if "not correct" in message else 400
            )
            self._send(status, {"ok": False, "error": message})
            return
        except Exception:  # noqa: BLE001 - never leak an internal failure to a caller
            self._send(500, {"ok": False, "error": "pairing failed on the gateway"})
            return
        self._send(200, payload)


def main(argv: list[str] | None = None) -> int:
    """Run the endpoint: `python -m hermes_gateway.pairing_server --state <dir> --dial <address>`."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="hermes-pairing",
        description="Serve the gateway's pairing endpoint: prove the words, issue a device identity.",
    )
    parser.add_argument("--state", default="state", help="the gateway's state directory")
    parser.add_argument("--dial", required=True,
                        help="the address devices dial, e.g. tls://gateway.example:10702")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=10704,
                        help="the pairing port; devices keep dialling the registry port instead")
    parser.add_argument("--certfile", default=None, help="defaults to <state>/registry-tls/server.crt")
    parser.add_argument("--keyfile", default=None, help="defaults to <state>/registry-tls/server.key")
    parser.add_argument("--check", action="store_true",
                        help="verify the state, the words and the certificate, then exit")
    parser.add_argument("--write-unit", default=None, metavar="PATH",
                        help="write a systemd user unit for this endpoint, then exit")
    args = parser.parse_args(argv)

    state = Path(args.state)
    tls = state / "registry-tls"
    certfile = Path(args.certfile) if args.certfile else tls / "server.crt"
    keyfile = Path(args.keyfile) if args.keyfile else tls / "server.key"

    config = PairingConfig(state_dir=state, dial_address=args.dial)
    service = PairingService(config)

    problems = []
    if not config.token_path().exists():
        problems.append(f"no pairing words at {config.token_path()}")
    for needed in (certfile, keyfile, tls / "client-ca.crt"):
        if not needed.exists():
            problems.append(f"missing {needed}")
    if problems:
        for problem in problems:
            print(f"  {problem}")
        print("  Run the gateway install first, then start this again.")
        return 2

    print(f"  pairing words: present ({len(config.token_path().read_text().split())} words)")
    print(f"  device pin   : {service.server_pin_hex()}")
    print(f"  devices dial : {config.dial_address}")
    print(f"  pairing at   : {args.bind}:{args.port} (this is the port the installer asks for)")
    if args.write_unit:
        import sys

        from .unit import UnitConfig, enable_instructions, write_unit

        target = write_unit(
            UnitConfig(python=Path(sys.executable), state_dir=state, dial_address=args.dial,
                       working_directory=Path.cwd(), bind=args.bind, port=args.port),
            Path(args.write_unit),
        )
        print(f"  wrote {target}")
        for line in enable_instructions(target):
            print(f"    {line}")
        return 0
    if args.check:
        print("  check only: nothing started")
        return 0
    return serve_pairing(service, bind=args.bind, port=args.port,
                         certfile=certfile, keyfile=keyfile)


def serve_pairing(
    service: PairingService,
    *,
    bind: str = "0.0.0.0",
    #: A separate port from the device registry, which already listens on its own. Devices dial the
    #: registry; only a workbench pairing a device talks here, and the two must not contend.
    port: int = 10704,
    certfile: Path,
    keyfile: Path,
) -> int:
    handler = type("BoundPairing", (PairingHandler,), {"service": service})
    server = ThreadingHTTPServer((bind, port), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"pairing endpoint listening on {bind}:{port} (TLS)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
