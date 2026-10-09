#!/usr/bin/env python3
"""Offline checks for the gateway half of plug-and-play satellite enrolment (2026-10-08).

Run with the project's interpreter (no pytest here):

    ./.venv/bin/python -B scripts/test_enroll_gateway.py

What is pinned:

  1  the bundled word list: 2048 entries, lowercase, unique, no digits
  2  token shape: five words joined by `-`, every one from the word list
  3  the entropy source: `secrets.choice` picks the words, `random` never does, and the token
     file is written mode 0600 (env override VOICE_ENROLL_TOKEN wins)
  4  GET /enroll/challenge returns one 64-hex nonce
  5  a challenge is single-use and expires after its TTL
  6  a bad, missing, or reused proof is 401 (one message, no oracle)
  7  a bad device_id is 400, and force must be a boolean
  8  already enrolled is 409 without force (by certificate file or by credential entry)
  9  the per-IP rate limit fires (10/min) and the global one fires across IPs
 10  a REAL round trip over real HTTP: real openssl minting via the existing issuer, an
     independently-implemented client that computes the proof and decrypts the bundle, and the
     registered fingerprint byte-identical to bridge/registry.py's peer_fingerprint form
 11  a re-enrolment with force reissues and supersedes the old credential
 12  the token and the proof never appear in a log record
 13  install-time generation: scripts/generate-enroll-token.py writes 0600 and never overwrites
 14  the wiring: the routes are registered on the bridge's existing announce server

The client side of check 10 is written straight from the wire contract (HMAC context, HKDF
salt/info, AES-256-GCM) instead of importing the server's helpers: a server that drifted from
the contract would fail here rather than pass against itself.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
from cryptography.exceptions import InvalidTag  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: E402

from bridge.enroll import (  # noqa: E402
    EnrollmentService, generate_token, load_token, load_wordlist,
)
from bridge.config import ENROLL_TOKEN_FILE  # noqa: E402

WORDS = REPO / "scripts" / "data" / "enroll-words.txt"
ISSUER = REPO / "scripts" / "issue-registry-tls.py"
GENERATOR = REPO / "scripts" / "generate-enroll-token.py"
PY = sys.executable

# Expected refusals log warnings; give logging a handler so the run's output is the checks only.
# Check 10 installs its own capture handler on top of this.
logging.getLogger().addHandler(logging.NullHandler())


# -- the contract, re-implemented here as the CLIENT would -------------------

def client_proof(token: str, nonce: str) -> str:
    message = ("enroll-v1:" + nonce).encode("utf-8")
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def client_open(token: str, response: dict) -> dict:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"hermes-enroll-v1",
               info=b"bundle").derive(token.encode("utf-8"))
    nonce = base64.b64decode(response["nonce"])
    sealed = base64.b64decode(response["ciphertext"]) + base64.b64decode(response["tag"])
    return json.loads(AESGCM(key).decrypt(nonce, sealed, None))


def pem_to_der(pem: str) -> bytes:
    body = b"".join(line for line in pem.encode().splitlines() if not line.startswith(b"-----"))
    return base64.b64decode(body)


# -- harness -----------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRequest:
    remote = "127.0.0.1"

    def __init__(self, body=None, *, proof: str | None = None, remote: str = "127.0.0.1") -> None:
        self._body = body
        self.headers = {"Authorization": f"Bearer {proof}"} if proof is not None else {}
        self.remote = remote

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def payload(response) -> dict:
    return json.loads(response.text)


class Harness:
    """An enrolment service with its own state dir, an initialized CA, and a live HTTP server.

    The default rate budgets are generous so unrelated checks never trip the limiter; check 9
    constructs its own with the real per-IP/global numbers.
    """

    def __init__(self, tmp: pathlib.Path, *, clock=None,
                 rate_per_minute: int = 1000, global_per_minute: int = 1000) -> None:
        self.root = tmp
        self.state = tmp / "state"
        self.tls = self.state / "registry-tls"
        self.token_file = self.state / "enroll-token.txt"
        self.credentials = self.state / "device-credentials.json"
        self.clock = clock or FakeClock()
        self.service = EnrollmentService(
            token_file=self.token_file, words_file=WORDS,
            token_env="_ENROLL_TEST_TOKEN_NEVER_SET",
            tls_dir=self.tls, credentials_file=self.credentials,
            rate_per_minute=rate_per_minute, global_per_minute=global_per_minute,
            clock=self.clock,
        )
        self.app = web.Application()
        self.service.register(self.app)     # also generates the token lazily, like the bridge
        self.runner = None
        self.session = None
        self.port = free_port()

    @property
    def token(self) -> str:
        return self.token_file.read_text().strip()

    def init_ca(self) -> None:
        result = subprocess.run(
            [PY, "-B", str(ISSUER), "--state", str(self.state), "init", "--san", "localhost"],
            capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        self.session = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self.session is not None:
            await self.session.close()
        if self.runner is not None:
            await self.runner.cleanup()

    async def get_challenge(self) -> dict:
        async with self.session.get(f"http://127.0.0.1:{self.port}/enroll/challenge") as reply:
            assert reply.status == 200, reply.status
            return await reply.json()

    async def post_enroll(self, body: dict, proof: str | None):
        headers = {"Authorization": f"Bearer {proof}"} if proof is not None else {}
        async with self.session.post(f"http://127.0.0.1:{self.port}/enroll",
                                     json=body, headers=headers) as reply:
            return reply.status, await reply.json()


# -- checks ------------------------------------------------------------------

TESTS: list = []


def check(number: str, description: str):
    def wrap(fn):
        TESTS.append((number, description, fn))
        return fn
    return wrap


@check("1", "the bundled word list is 2048 clean, unique, lowercase words")
def t_wordlist() -> None:
    words = load_wordlist(WORDS)
    assert len(words) == 2048, f"expected 2048 entries, got {len(words)}"
    assert len(set(words)) == len(words), "duplicate words in the list"
    for word in words:
        assert re.fullmatch(r"[a-z]{3,8}", word), f"unclean entry: {word!r}"


@check("2", "a token is five words from the list, joined by '-'")
def t_token_shape() -> None:
    words = load_wordlist(WORDS)
    token = generate_token(words)
    parts = token.split("-")
    assert len(parts) == 5, token
    assert all(part in words for part in parts), token
    assert re.fullmatch(r"[a-z]+(-[a-z]+){4}", token), token
    assert generate_token(words) != token, "two tokens should never collide"


@check("3", "secrets.choice picks the words, random never does, and the file is 0600")
def t_entropy_source() -> None:
    words = load_wordlist(WORDS)
    source = (REPO / "bridge" / "enroll.py").read_text()
    assert "import random" not in source and "from random" not in source, \
        "the enrolment token must never be generated from random"
    assert "secrets.choice" in source
    # The pick is resolved at call time from `secrets`, so patching it proves the source.
    picks: list = []
    original = secrets.choice

    def fake_choice(seq):
        picks.append(seq)
        return original(seq)

    secrets.choice = fake_choice
    try:
        generate_token(words)
    finally:
        secrets.choice = original
    assert len(picks) == 5, f"expected 5 secrets.choice picks, got {len(picks)}"

    with tempfile.TemporaryDirectory() as tmp:
        token_file = pathlib.Path(tmp) / "state" / "enroll-token.txt"
        token = load_token(token_file=token_file, words_file=WORDS,
                           env_name="_ENROLL_TEST_TOKEN_NEVER_SET")
        assert token_file.read_text().strip() == token
        assert len(token.split("-")) == 5
        assert (token_file.stat().st_mode & 0o777) == 0o600, oct(token_file.stat().st_mode)
        # Generated once: a second load returns the same token and rewrites nothing.
        assert load_token(token_file=token_file, words_file=WORDS,
                          env_name="_ENROLL_TEST_TOKEN_NEVER_SET") == token
        # The environment override wins over the file (VOICE_ENROLL_TOKEN's shape).
        os.environ["_ENROLL_TEST_TOKEN_OVERRIDE"] = "alpha-beta-gamma-delta-epsilon"
        try:
            overridden = load_token(token_file=token_file, words_file=WORDS,
                                    env_name="_ENROLL_TEST_TOKEN_OVERRIDE")
        finally:
            del os.environ["_ENROLL_TEST_TOKEN_OVERRIDE"]
        assert overridden == "alpha-beta-gamma-delta-epsilon", overridden
    assert ENROLL_TOKEN_FILE.name == "enroll-token.txt", ENROLL_TOKEN_FILE


@check("4", "GET /enroll/challenge returns one 64-hex nonce")
def t_challenge_shape() -> None:
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(pathlib.Path(tmp))
            try:
                await h.start()
                body = await h.get_challenge()
                assert set(body) == {"nonce"}, body
                assert re.fullmatch(r"[0-9a-f]{64}", body["nonce"]), body
                second = await h.get_challenge()
                assert second["nonce"] != body["nonce"], "nonces must be per-connection random"
            finally:
                await h.stop()
    asyncio.run(run())


@check("5", "a challenge is single-use and expires after its TTL")
def t_challenge_single_use_and_ttl() -> None:
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(pathlib.Path(tmp))
            h.service._token = "alpha-beta-gamma-delta-epsilon"   # fixed, so proofs are computable
            clock = h.clock
            nonce = "a" * 64
            h.service._challenges[nonce] = clock()
            proof = client_proof(h.service._token, nonce)
            assert h.service._consume_challenge(proof) == nonce
            assert h.service._consume_challenge(proof) is None, "a used challenge must not match again"

            fresh = "b" * 64
            h.service._challenges[fresh] = clock()
            clock.advance(61.0)
            h.service._prune_challenges()
            assert fresh not in h.service._challenges, "an expired challenge must be pruned"
            assert h.service._consume_challenge(client_proof(h.service._token, fresh)) is None, \
                "an expired challenge must not answer a proof"
    asyncio.run(run())


@check("6", "a bad, missing, or reused proof is 401 with one indistinguishable message")
def t_bad_proof() -> None:
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(pathlib.Path(tmp))
            await h.start()
            try:
                nonce = (await h.get_challenge())["nonce"]
                good = client_proof(h.token, nonce)
                wrong_key = client_proof("wrong-token-words-right-here", nonce)
                wrong_nonce = client_proof(h.token, "c" * 64)

                for label, proof in (("no header", None), ("wrong key", wrong_key),
                                     ("wrong nonce", wrong_nonce), ("empty", "")):
                    status, body = await h.post_enroll({"device_id": "esp32-x"}, proof)
                    assert status == 401, f"{label}: {status} {body}"
                    assert body.get("error") == "unauthorized", body

                # The real proof is spent by being presented, even though the request below is
                # refused (no CA in this state dir): a captured exchange is never replayable.
                status, body = await h.post_enroll({"device_id": "esp32-x"}, good)
                assert status == 503, (status, body)      # valid proof, uninitialised gateway
                status, body = await h.post_enroll({"device_id": "esp32-x"}, good)
                assert status == 401, f"reused proof must be refused: {status} {body}"
            finally:
                await h.stop()
    asyncio.run(run())


@check("7", "a bad device_id is 400, and force must be a boolean")
def t_bad_device_id() -> None:
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(pathlib.Path(tmp))
            await h.start()
            try:
                for body in ({"device_id": "bad/id"}, {"device_id": "../escape"},
                             {"device_id": "x" * 65}, {"device_id": ""}, {},
                             {"device_id": 42}, {"device_id": "esp32-ok", "force": "yes"}):
                    nonce = (await h.get_challenge())["nonce"]
                    status, reply = await h.post_enroll(body, client_proof(h.token, nonce))
                    assert status == 400, f"{body}: {status} {reply}"
            finally:
                await h.stop()
    asyncio.run(run())


@check("8", "already enrolled is 409 without force (by file or by credential entry)")
def t_already_enrolled() -> None:
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(pathlib.Path(tmp))
            await h.start()
            try:
                # By certificate file: a device the issuer has already minted.
                h.tls.mkdir(parents=True, exist_ok=True)
                (h.tls / "esp32-file-1.crt").write_text("stub")
                nonce = (await h.get_challenge())["nonce"]
                status, body = await h.post_enroll({"device_id": "esp32-file-1"},
                                                   client_proof(h.token, nonce))
                assert status == 409, (status, body)
                assert "already enrolled" in body["error"], body

                # By credential entry alone: named in device-credentials.json, no file.
                h.credentials.write_text(json.dumps({"sha256:00": "esp32-named-1"}))
                nonce = (await h.get_challenge())["nonce"]
                status, body = await h.post_enroll({"device_id": "esp32-named-1"},
                                                   client_proof(h.token, nonce))
                assert status == 409, (status, body)

                # A different device is not affected by either.
                nonce = (await h.get_challenge())["nonce"]
                status, body = await h.post_enroll({"device_id": "esp32-fresh-1"},
                                                   client_proof(h.token, nonce))
                assert status == 503, f"expected the uninitialised-gateway refusal: {status} {body}"
            finally:
                await h.stop()
    asyncio.run(run())


@check("9", "the per-IP rate limit fires at 10/min, and the global one fires across IPs")
def t_rate_limit() -> None:
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(pathlib.Path(tmp), rate_per_minute=10, global_per_minute=30)
            await h.start()
            try:
                url = f"http://127.0.0.1:{h.port}/enroll/challenge"
                # One IP: ten pass, the eleventh is throttled while the global budget is full.
                statuses = []
                for _ in range(11):
                    async with h.session.get(url) as reply:
                        statuses.append(reply.status)
                assert statuses[:10] == [200] * 10, statuses
                assert statuses[10] == 429, statuses
                async with h.session.get(url) as reply:
                    body = await reply.json()
                assert reply.status == 429 and "rate limit" in body["error"], body

                # The per-IP bucket is per CLIENT ADDRESS: distinct remotes keep their own
                # budgets while the exhausted one stays throttled, and the global bucket of 30
                # still bounds all of them together.
                with tempfile.TemporaryDirectory() as tmp2:
                    h2 = Harness(pathlib.Path(tmp2), rate_per_minute=10, global_per_minute=30)
                    seen = []

                    async def fire(remote: str):
                        seen.append((await h2.service.handle_challenge(
                            FakeRequest(remote=remote))).status)

                    # 10 + 10 + 10 fit the global budget exactly; a fourth address with a full
                    # per-IP bucket of its own is still refused, which is the global limit firing.
                    for remote in ("10.0.0.1",) * 10 + ("10.0.0.2",) * 10 + ("10.0.0.3",) * 10:
                        await fire(remote)
                    assert seen == [200] * 30, seen
                    await fire("10.0.0.4")
                    assert seen[-1] == 429, f"the global bucket must fire: {seen}"
                    await fire("10.0.0.4")
                    assert seen[-1] == 429, seen

                # /enroll is throttled by the same budget: a fresh service, spent on /enroll.
                with tempfile.TemporaryDirectory() as tmp3:
                    h3 = Harness(pathlib.Path(tmp3), rate_per_minute=10, global_per_minute=30)
                    results = []
                    for _ in range(11):
                        results.append((await h3.service.handle_enroll(
                            FakeRequest({"device_id": "esp32-x"}, proof="00"))).status)
                    assert results == [401] * 10 + [429], results
            finally:
                await h.stop()
    asyncio.run(run())


@check("10", "REAL round trip: challenge, proof, openssl mint, AES-256-GCM decrypt")
def t_real_round_trip() -> None:
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(pathlib.Path(tmp))
            h.init_ca()
            captured: list[str] = []

            class Capture(logging.Handler):
                def emit(self, record):
                    captured.append(record.getMessage())

            bridge_log = logging.getLogger("bridge")
            previous_level = bridge_log.level
            capture = Capture()
            bridge_log.setLevel(logging.INFO)      # so the enrolment INFO line is observable
            bridge_log.addHandler(capture)
            await h.start()
            try:
                device_id = "esp32-68ee8f4b5988"
                nonce = (await h.get_challenge())["nonce"]
                proof = client_proof(h.token, nonce)          # the client's own HMAC
                status, body = await h.post_enroll({"device_id": device_id}, proof)
                assert status == 200, (status, body)

                # The wire message: exactly the three contract fields, nonce 24 b64 chars.
                assert set(body) == {"ciphertext", "tag", "nonce"}, body
                assert len(body["nonce"]) == 24, body["nonce"]
                assert len(base64.b64decode(body["tag"])) == 16

                bundle = client_open(h.token, body)           # the client's own HKDF + GCM
                assert set(bundle) == {"client_ca", "cert", "key"}, set(bundle)
                for name in ("client_ca", "cert", "key"):
                    assert bundle[name].startswith("-----BEGIN "), bundle[name][:30]

                # The bundle is the very material minted on disk by the existing issuer.
                assert bundle["client_ca"] == (h.tls / "client-ca.crt").read_text()
                assert bundle["cert"] == (h.tls / f"{device_id}.crt").read_text()
                assert bundle["key"] == (h.tls / f"{device_id}.key").read_text()

                # The registered fingerprint is byte-identical to what bridge/registry.py's
                # peer_fingerprint computes from a live TLS peer: sha256 over the DER.
                creds = json.loads(h.credentials.read_text())
                der_fingerprint = "sha256:" + hashlib.sha256(
                    ssl.PEM_cert_to_DER_cert(bundle["cert"])).hexdigest()
                assert der_fingerprint in creds, creds
                assert creds[der_fingerprint] == device_id
                # ... and identical to the provisioner's own pem_to_der path.
                assert "sha256:" + hashlib.sha256(pem_to_der(bundle["cert"])).hexdigest() \
                    == der_fingerprint

                # Only the token holder can open it: a different token fails GCM authentication.
                try:
                    client_open("wrong-token-words-right-here", body)
                    raise AssertionError("a wrong token must not open the bundle")
                except InvalidTag:
                    pass

                # A reused proof is refused after success too.
                status, body = await h.post_enroll({"device_id": device_id}, proof)
                assert status == 401, (status, body)

                # The issuer itself calls the minted set consistent (real openssl verify).
                verified = subprocess.run(
                    [PY, "-B", str(ISSUER), "--state", str(h.state), "verify"],
                    capture_output=True, text=True)
                assert verified.returncode == 0, verified.stdout + verified.stderr

                # Hygiene: neither the token nor the proof nor the key was ever logged.
                joined = "\n".join(captured)
                assert h.token not in joined, "the enrolment token was logged"
                assert proof not in joined, "the proof was logged"
                assert bundle["key"] not in joined, "the device key was logged"
                assert captured, "expected the enrolment to be logged at all"
            finally:
                bridge_log.removeHandler(capture)
                bridge_log.setLevel(previous_level)
                await h.stop()
    asyncio.run(run())


@check("11", "a re-enrolment with force reissues and supersedes the old credential")
def t_force_reissue() -> None:
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(pathlib.Path(tmp))
            h.init_ca()
            await h.start()
            try:
                device_id = "esp32-reissue-1"

                async def enrol(force=None):
                    nonce = (await h.get_challenge())["nonce"]
                    body = {"device_id": device_id}
                    if force is not None:
                        body["force"] = force
                    return await h.post_enroll(body, client_proof(h.token, nonce))

                status, first = await enrol()
                assert status == 200, (status, first)
                first_fp = json.loads(h.credentials.read_text())

                status, body = await enrol()
                assert status == 409, f"a second enrolment must be refused: {status} {body}"

                status, second = await enrol(force=True)
                assert status == 200, (status, second)
                second_fp = json.loads(h.credentials.read_text())
                assert second_fp != first_fp, "a reissue must register a NEW fingerprint"
                assert len(second_fp) == 1, second_fp
                assert list(second_fp.values()) == [device_id]
                assert client_open(h.token, second) != client_open(h.token, first)
            finally:
                await h.stop()
    asyncio.run(run())


@check("12", "the install-time generator writes 0600 and never overwrites")
def t_generator_script() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "state" / "enroll-token.txt"
        first = subprocess.run([PY, "-B", str(GENERATOR), "--out", str(out)],
                               capture_output=True, text=True)
        assert first.returncode == 0, first.stdout + first.stderr
        token = first.stdout.strip()
        assert len(token.split("-")) == 5, token
        assert out.read_text().strip() == token
        assert (out.stat().st_mode & 0o777) == 0o600
        again = subprocess.run([PY, "-B", str(GENERATOR), "--out", str(out)],
                               capture_output=True, text=True)
        assert again.returncode == 1, "a second run must refuse to overwrite the token"
        assert out.read_text().strip() == token
        assert token not in again.stdout, "a kept token must never be re-printed"


@check("13", "install-gateway.sh generates the token at install and says it is needed once")
def t_install_gateway_step() -> None:
    source = (REPO / "scripts" / "install-gateway.sh").read_text()
    assert "generate-enroll-token.py" in source
    assert "enroll-token.txt" in source
    assert "you will need this when you set up a satellite" in source.lower()
    # Printed ONCE: a re-run reports the file exists and prints no token.
    assert "never re-printed" in source.lower()


@check("14", "the routes are registered on the bridge's existing announce server")
def t_wiring() -> None:
    source = (REPO / "bridge" / "announce.py").read_text()
    assert "self.enrollment.register(app)" in source, \
        "the announce server must register the enrolment routes"
    assert "EnrollmentService()" in source
    enroll_source = (REPO / "bridge" / "enroll.py").read_text()
    assert 'add_get("/enroll/challenge"' in enroll_source
    assert 'add_post("/enroll"' in enroll_source


# -- runner ------------------------------------------------------------------

async def amain() -> int:
    failures = 0
    for number, description, fn in TESTS:
        try:
            if asyncio.iscoroutinefunction(fn):
                await asyncio.wait_for(fn(), 120)
            else:
                await asyncio.wait_for(asyncio.to_thread(fn), 120)
            print(f"  ok    {number:<4} {description}")
        except Exception as exc:  # noqa: BLE001 - a test failure is data, not a crash
            failures += 1
            print(f"  FAIL  {number:<4} {description}")
            print(f"        {type(exc).__name__}: {exc}")
    print(f"\nenrol gateway checks: {len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
