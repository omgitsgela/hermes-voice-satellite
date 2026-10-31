"""Plug-and-play satellite enrolment: the gateway half (2026-10-08).

A fresh board has no secret at all, so enrolment carries the OPERATOR's authority: a five-word
token the gateway generates once, the person types into the installer, and neither side ever
sends over the wire. The token proves knowledge (an HMAC of a single-use challenge) and wraps
the answer (AES-256-GCM under a key derived from the same token).

Wire protocol (the contract; scripts/test_enroll_gateway.py pins it):

    GET  /enroll/challenge
      -> 200 {"nonce": "<64 hex>"}          # single use, 60 s TTL, per-connection random
         429 if throttled

    POST /enroll
      Authorization: Bearer <proof>
      {"device_id": "esp32-68ee8f4b5988"}
      where proof = hex(HMAC-SHA256(key = token, msg = "enroll-v1:" + nonce))
      -> 200 {"ciphertext": "<b64>", "tag": "<b64>", "nonce": "<24 b64 chars>"}
         401 bad or reused proof        400 bad device_id        409 already enrolled (no force)
         429 throttled

    ciphertext = AES-256-GCM(key, nonce).encrypt(plaintext) where
        key       = HKDF-SHA256(ikm = token.encode(), salt = b"hermes-enroll-v1", info = b"bundle")
        plaintext = json({"client_ca": "<PEM>", "cert": "<PEM>", "key": "<PEM>", "server": "<PEM>"}).encode()

    `ciphertext` is the bare ciphertext and `tag` the 16-byte GCM tag, kept apart because the
    contract names them apart; the opener rejoins them (ciphertext + tag) for AESGCM.decrypt.
    The response nonce is 16 raw bytes, base64-encoded to exactly 24 characters, which is the
    "<24 b64 bytes>" the contract states. The key material never crosses the wire in the clear
    and the token never crosses it at all.

Why this shape: a passive sniffer sees only an HMAC and learns nothing replayable (the nonce is
single-use); an impostor cannot forge a response (GCM authentication fails under the derived
key), so no server pinning is needed and the five words are the only thing typed.

Hardening, on both calls: a per-IP and a global token bucket with a clear 429, `hmac.compare_digest`
for every secret comparison, and the token / proof / key material never appear in a log line, an
error body, or a response field.

Minting is NOT implemented here: `POST /enroll` invokes the existing issuer
(scripts/issue-registry-tls.py) so the fingerprint registered in `device-credentials.json` stays
byte-identical to what gateway/registry.py's `peer_fingerprint` computes from a live TLS peer.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import importlib.util
import io
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from aiohttp import web
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from gateway.config import (
    ENROLL_CHALLENGE_TTL_SECONDS,
    ENROLL_GLOBAL_RATE_PER_MINUTE,
    ENROLL_RATE_PER_MINUTE,
    ENROLL_TOKEN_ENV,
    ENROLL_TOKEN_FILE,
    ENROLL_WORDS_FILE,
    PROJECT_DIR,
    REGISTRY_CREDENTIALS_FILE,
    REGISTRY_TLS_CERT_FILE,
)

_LOGGER = logging.getLogger("gateway.enroll")

TOKEN_WORDS = 5          # five dictionary words: ~55 bits from a 2048-entry list, typeable
CHALLENGE_BYTES = 32     # 64 hex characters
PROOF_CONTEXT = "enroll-v1:"
HKDF_SALT = b"hermes-enroll-v1"
HKDF_INFO = b"bundle"
GCM_NONCE_BYTES = 16     # base64 of 16 bytes is exactly 24 characters, as the contract states
GCM_TAG_BYTES = 16
MAX_DEVICE_ID = 64
ISSUER_SCRIPT = PROJECT_DIR / "scripts" / "issue-registry-tls.py"

BUNDLE_KEYS = ("client_ca", "cert", "key", "server")
RESPONSE_KEYS = ("ciphertext", "tag", "nonce")


# -- the token ---------------------------------------------------------------

def load_wordlist(path: Path = ENROLL_WORDS_FILE) -> list[str]:
    """The bundled word list: one lowercase word per line, `#` comments allowed."""
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise RuntimeError(f"cannot read the enrolment word list at {path}: {exc}") from exc
    words = [line.strip() for line in text.splitlines()
             if line.strip() and not line.strip().startswith("#")]
    if len(words) < TOKEN_WORDS:
        raise RuntimeError(f"the enrolment word list at {path} has only {len(words)} words")
    return words


def generate_token(wordlist: Sequence[str], chooser: Optional[Callable] = None) -> str:
    """Five words joined by `-`, every one picked with `secrets.choice` (never `random`).

    `chooser` exists so tests can drive the pick deterministically; production never passes it.
    """
    if len(wordlist) < TOKEN_WORDS:
        raise ValueError(f"need at least {TOKEN_WORDS} words, got {len(wordlist)}")
    pick = chooser if chooser is not None else secrets.choice
    return "-".join(pick(wordlist) for _ in range(TOKEN_WORDS))


def load_token(*, token_file: Path = ENROLL_TOKEN_FILE, words_file: Path = ENROLL_WORDS_FILE,
               env_name: str = ENROLL_TOKEN_ENV) -> str:
    """Env override, else the token file, else generate one -- the announce token's shape.

    A generated token is written mode 600 and never printed from here: `install-gateway.sh`
    prints it ONCE at install time, and a Gateway that generates it lazily leaves it for the
    operator to read from the file. Never log the value.
    """
    from_env = os.environ.get(env_name, "").strip() or ""
    if from_env:
        return from_env
    try:
        existing = Path(token_file).read_text().strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:  # noqa: BLE001
        _LOGGER.warning("could not read %s: %s", token_file, exc)
        existing = ""
    if existing:
        return existing
    token = generate_token(load_wordlist(words_file))
    Path(token_file).parent.mkdir(parents=True, exist_ok=True)
    Path(token_file).write_text(token + "\n")
    Path(token_file).chmod(0o600)
    _LOGGER.info("generated a satellite enrolment token at %s (mode 600)", token_file)
    return token


# -- the crypto contract -----------------------------------------------------

def proof_for(token: str, nonce: str) -> str:
    """hex(HMAC-SHA256(key = token, msg = "enroll-v1:" + nonce)) -- what the client presents."""
    message = (PROOF_CONTEXT + nonce).encode("utf-8")
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def derive_bundle_key(token: str) -> bytes:
    """HKDF-SHA256(ikm = token.encode(), salt = b"hermes-enroll-v1", info = b"bundle")."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=HKDF_SALT,
                info=HKDF_INFO).derive(token.encode("utf-8"))


def seal_bundle(token: str, plaintext: bytes) -> dict:
    """AES-256-GCM under the derived key: ciphertext and tag kept apart, nonce in the reply.

    The nonce is 16 fresh random bytes (base64: exactly 24 characters). Random per message, so
    two enrolments never repeat a (key, nonce) pair.
    """
    nonce = secrets.token_bytes(GCM_NONCE_BYTES)
    sealed = AESGCM(derive_bundle_key(token)).encrypt(nonce, plaintext, None)
    ciphertext, tag = sealed[:-GCM_TAG_BYTES], sealed[-GCM_TAG_BYTES:]
    return {
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }


# -- rate limiting -----------------------------------------------------------

class TokenBucket:
    """A refilling token bucket over a monotonic clock."""

    def __init__(self, capacity: int, per_minute: float, clock: Callable[[], float]) -> None:
        self.capacity = max(1, int(capacity))
        self.rate_per_second = max(1, int(per_minute)) / 60.0
        self._clock = clock
        self.tokens = float(self.capacity)
        self.updated = clock()

    def allow(self) -> bool:
        now = self._clock()
        self.tokens = min(float(self.capacity),
                          self.tokens + max(0.0, now - self.updated) * self.rate_per_second)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RateLimiter:
    """A global bucket and one bucket per client IP; a request needs a token from both.

    The global bucket is larger than the per-IP one on purpose: a global budget equal to the
    per-IP budget would let one noisy (or hostile) client lock every other device out of
    enrolment. The per-IP bucket is the brute-force brake; the global one bounds the endpoint
    as a whole.
    """

    def __init__(self, per_minute: int = ENROLL_RATE_PER_MINUTE,
                 global_per_minute: int = ENROLL_GLOBAL_RATE_PER_MINUTE,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._global = TokenBucket(global_per_minute, global_per_minute, clock)
        self._per_ip: dict[str, TokenBucket] = {}
        self._per_minute = per_minute

    def allow(self, ip: str) -> bool:
        bucket = self._per_ip.get(ip)
        if bucket is None:
            bucket = TokenBucket(self._per_minute, self._per_minute, self._clock)
            self._per_ip[ip] = bucket
        # Both budgets are consulted before either is spent, so a refused request does not
        # quietly eat another client's global token.
        if not self._global.allow() or not bucket.allow():
            return False
        return True


# -- the issuer (minting is reused, never re-implemented) ---------------------

_issuer_module = None


def _issuer():
    """scripts/issue-registry-tls.py, loaded once: the ONE producer of device certificates.

    cmd_enroll is invoked rather than rewritten so the fingerprint this endpoint registers in
    `device-credentials.json` is computed by the same code the manual flow uses, and stays
    byte-identical to gateway/registry.py's `peer_fingerprint`.
    """
    global _issuer_module
    if _issuer_module is None:
        spec = importlib.util.spec_from_file_location("issue_registry_tls", ISSUER_SCRIPT)
        assert spec is not None and spec.loader is not None, f"cannot load {ISSUER_SCRIPT}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _issuer_module = module
    return _issuer_module


class MintError(Exception):
    """The issuer refused or could not run: surfaced as a 503, never a stack trace."""


# -- the service -------------------------------------------------------------

class EnrollmentService:
    """`GET /enroll/challenge` + `POST /enroll`, wired into the Gateway's existing LAN server."""

    def __init__(self, *, token_file: Path = ENROLL_TOKEN_FILE,
                 words_file: Path = ENROLL_WORDS_FILE, token_env: str = ENROLL_TOKEN_ENV,
                 tls_dir: Optional[Path] = None, credentials_file: Optional[Path] = None,
                 rate_per_minute: int = ENROLL_RATE_PER_MINUTE,
                 global_per_minute: int = ENROLL_GLOBAL_RATE_PER_MINUTE,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.token_file = Path(token_file)
        self.words_file = Path(words_file)
        self.token_env = token_env
        self.tls_dir = Path(tls_dir) if tls_dir is not None else REGISTRY_TLS_CERT_FILE.parent
        self.credentials_file = (Path(credentials_file) if credentials_file is not None
                                 else REGISTRY_CREDENTIALS_FILE)
        self._clock = clock
        self._token = ""
        self._challenges: dict[str, float] = {}   # nonce -> issued (monotonic)
        self.rate = RateLimiter(per_minute=rate_per_minute, global_per_minute=global_per_minute,
                                clock=clock)

    # -- lifecycle ----------------------------------------------------------

    def register(self, app: web.Application) -> None:
        """Add the two routes beside the announce endpoint's, loading the token lazily."""
        self._token = load_token(token_file=self.token_file, words_file=self.words_file,
                                 env_name=self.token_env)
        app.router.add_get("/enroll/challenge", self.handle_challenge)
        app.router.add_post("/enroll", self.handle_enroll)

    # -- request plumbing ---------------------------------------------------

    def _throttled(self, request: web.Request) -> Optional[web.Response]:
        if self.rate.allow(request.remote or "unknown"):
            return None
        _LOGGER.warning("enrolment rate limit hit by %s", request.remote)
        return web.json_response(
            {"ok": False, "error": "rate limit exceeded; retry in a minute"}, status=429)

    @staticmethod
    def _bearer(request: web.Request) -> Optional[str]:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        presented = header[len("Bearer "):].strip()
        return presented or None

    def _prune_challenges(self) -> None:
        cutoff = self._clock() - ENROLL_CHALLENGE_TTL_SECONDS
        for nonce, issued in list(self._challenges.items()):
            if issued <= cutoff:
                del self._challenges[nonce]

    def _consume_challenge(self, presented: str) -> Optional[str]:
        """The single outstanding challenge this proof answers, or None.

        Every live nonce is compared (constant-time) so the proof's own shape does not leak
        which challenge was taken. A match is REMOVED immediately: a proof is spent by being
        presented, success or not, so a captured exchange can never be replayed.
        """
        match = None
        for nonce in list(self._challenges):
            if hmac.compare_digest(proof_for(self._token, nonce), presented):
                match = nonce
        if match is not None:
            del self._challenges[match]
        return match

    # -- the endpoints ------------------------------------------------------

    async def handle_challenge(self, request: web.Request) -> web.Response:
        throttled = self._throttled(request)
        if throttled is not None:
            return throttled
        self._prune_challenges()
        nonce = secrets.token_hex(CHALLENGE_BYTES)
        self._challenges[nonce] = self._clock()
        return web.json_response({"nonce": nonce})

    async def handle_enroll(self, request: web.Request) -> web.Response:
        throttled = self._throttled(request)
        if throttled is not None:
            return throttled

        presented = self._bearer(request)
        if presented is None or self._consume_challenge(presented) is None:
            # One message for a bad proof and a reused one: the caller learns nothing about
            # which challenge was live. Never log the proof itself.
            _LOGGER.warning("rejected an enrolment proof from %s", request.remote)
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - a malformed body is a 400, never a 500
            return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"},
                                     status=400)
        device_id = body.get("device_id")
        wanted = device_id if isinstance(device_id, str) else ""
        if not self._valid_device_id(wanted):
            return web.json_response(
                {"ok": False,
                 "error": "device_id is required and may contain only letters, digits, '-' "
                          f"and '_' (max {MAX_DEVICE_ID} characters)"}, status=400)
        device_id = wanted
        force = body.get("force", False)
        if not isinstance(force, bool):
            return web.json_response({"ok": False, "error": "force must be true or false"},
                                     status=400)

        if not force and self._already_enrolled(device_id):
            _LOGGER.info("refused a second enrolment of %s from %s (already enrolled)",
                         device_id, request.remote)
            return web.json_response(
                {"ok": False,
                 "error": f"{device_id} is already enrolled; pass force to reissue its "
                          "certificate"}, status=409)

        if not (self.tls_dir / "ca.crt").is_file() or not (self.tls_dir / "ca.key").is_file():
            _LOGGER.error("enrolment requested but there is no CA at %s", self.tls_dir)
            return web.json_response(
                {"ok": False,
                 "error": "gateway TLS material is not initialised (run install-gateway.sh)"},
                status=503)

        try:
            self._mint(device_id, force=force)
        except MintError as exc:
            _LOGGER.error("could not mint a certificate for %s: %s", device_id, exc)
            return web.json_response(
                {"ok": False, "error": "could not issue a device certificate"}, status=503)

        # server.crt travels in the same protected bundle. `provision-device-tls.py` pins it
        # into the board as the gateway identity, so carrying it here means the client needs a
        # single connection and no TLS capture to obtain it -- and since the whole bundle is
        # AEAD-sealed under the token, an impostor still cannot substitute a server certificate.
        bundle = {
            "client_ca": (self.tls_dir / "client-ca.crt").read_text(),
            "cert": (self.tls_dir / f"{device_id}.crt").read_text(),
            "key": (self.tls_dir / f"{device_id}.key").read_text(),
            "server": (self.tls_dir / "server.crt").read_text(),
        }
        response = seal_bundle(self._token, json.dumps(bundle).encode("utf-8"))
        _LOGGER.info("enrolled %s from %s (certificate issued, credential registered)",
                     device_id, request.remote)
        return web.json_response(response)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _valid_device_id(device_id) -> bool:
        """The issuer's own id rule: letters, digits, '-' and '_' only. A '/' would be a path."""
        return (isinstance(device_id, str) and 0 < len(device_id) <= MAX_DEVICE_ID
                and device_id.isascii()
                and all(c.isalnum() or c in "-_" for c in device_id))

    def _already_enrolled(self, device_id: str) -> bool:
        """A device counts as enrolled when its certificate exists or a credential names it."""
        if (self.tls_dir / f"{device_id}.crt").is_file() or \
                (self.tls_dir / f"{device_id}.key").is_file():
            return True
        if self.credentials_file.is_file():
            try:
                creds = _issuer().load_credentials(self.credentials_file)
            except (SystemExit, OSError, ValueError) as exc:
                _LOGGER.warning("could not read %s: %s", self.credentials_file, exc)
                return False
            return device_id in creds.values()
        return False

    def _mint(self, device_id: str, *, force: bool) -> None:
        """Sign and register one device by running the existing issuer's own enroll command."""
        issuer = _issuer()
        if self.tls_dir.name != "registry-tls":
            raise MintError(
                f"the issuer mints into <state>/registry-tls but the TLS dir is {self.tls_dir}; "
                "keep the default layout or run issue-registry-tls.py by hand")
        args = argparse.Namespace(
            state=str(self.tls_dir.parent), new=device_id, device=None,
            credentials=str(self.credentials_file), days=issuer.DEFAULT_DAYS,
            force=force, endpoint_host=None, endpoint_port=None,
        )
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                issuer.cmd_enroll(args)
        except SystemExit as exc:
            raise MintError(str(exc)) from exc
