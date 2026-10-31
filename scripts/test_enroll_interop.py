#!/usr/bin/env python3
"""The cross-side round trip: the gateway's REAL /enroll service against the installer's REAL
client, in one run.

Both halves were written against a written contract, and a contract can be misread the same way by
both sides -- two mocks that agree with each other prove nothing about the real pair. So this
drives the real service (`gateway/enroll.py`, minting through the real `scripts/issue-registry-tls.py`)
against the real client (`scripts/enroll_device.py` as a subprocess), and asserts on the files the
board would actually be provisioned from.

Run:  .venv/bin/python -B scripts/test_enroll_interop.py
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402

from satellite_gateway.enroll import EnrollmentService  # noqa: E402

PY = sys.executable
ENROLL = ROOT / "scripts" / "enroll_device.py"
ISSUER = ROOT / "scripts" / "issue-registry-tls.py"
WORDS = ROOT / "scripts" / "data" / "enroll-words.txt"
DEVICE_ID = "esp32-interop-test"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServiceThread:
    """The enrolment service on its own event loop thread, so a real HTTP client can reach it."""

    def __init__(self, service: EnrollmentService, port: int) -> None:
        self.service = service
        self.port = port
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.started = threading.Event()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        finally:
            self.loop.run_forever()

    async def _serve(self) -> None:
        app = web.Application()
        self.service.register(app)          # also generates the token lazily, like the Gateway
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.port).start()
        self.started.set()

    def start(self) -> None:
        self.thread.start()
        assert self.started.wait(10), "the enrolment service did not start"

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)


class CrossSideTests(unittest.TestCase):
    """One enrolment, done end to end by the two real halves."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="enrol-interop-"))
        self.state = self.tmp / "state"
        self.tls = self.state / "registry-tls"
        self.credentials = self.state / "device-credentials.json"
        self.token_file = self.state / "enroll-token.txt"
        self.out = self.tmp / "certs"
        # The real issuer mints the CA + the Gateway's own server certificate.
        result = subprocess.run(
            [PY, "-B", str(ISSUER), "--state", str(self.state), "init", "--san", "localhost"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _service(self) -> EnrollmentService:
        return EnrollmentService(
            token_file=self.token_file, words_file=WORDS,
            token_env="_ENROLL_INTEROP_TOKEN_NEVER_SET",
            tls_dir=self.tls, credentials_file=self.credentials,
            rate_per_minute=1000, global_per_minute=1000)

    def test_the_real_client_opens_a_real_gateway_response(self):
        service = self._service()
        port = free_port()
        server = ServiceThread(service, port)
        server.start()
        try:
            result = subprocess.run(
                [PY, "-B", str(ENROLL),
                 "--device-id", DEVICE_ID,
                 "--gateway", f"http://127.0.0.1:{port}",
                 "--out", str(self.out),
                 "--token-file", str(self.token_file)],
                capture_output=True, text=True, timeout=60)
        finally:
            server.stop()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # The four files the board is provisioned from, plus the merged credential record.
        names = {path.name for path in self.out.iterdir()}
        self.assertEqual(names, {"client-ca.crt", f"{DEVICE_ID}.crt", f"{DEVICE_ID}.key",
                                 "server.crt", "device-credentials.json"})

        # The material the client wrote is byte-identical to what the gateway minted on disk.
        self.assertEqual((self.out / "client-ca.crt").read_text().strip(),
                         (self.tls / "client-ca.crt").read_text().strip())
        self.assertEqual((self.out / f"{DEVICE_ID}.crt").read_text().strip(),
                         (self.tls / f"{DEVICE_ID}.crt").read_text().strip())
        self.assertEqual((self.out / f"{DEVICE_ID}.key").read_text().strip(),
                         (self.tls / f"{DEVICE_ID}.key").read_text().strip())
        # server.crt came from the sealed bundle, not from a second TLS socket.
        self.assertEqual((self.out / "server.crt").read_text().strip(),
                         (self.tls / "server.crt").read_text().strip())
        self.assertIn("sealed bundle", result.stdout)

        # The private key is not world-readable.
        mode = (self.out / f"{DEVICE_ID}.key").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"the device key is mode {oct(mode)}")

        # The credential record is the Gateway's own shape: sha256 over the certificate DER.
        credentials = json.loads((self.out / "device-credentials.json").read_text())
        self.assertEqual(list(credentials.values()), [DEVICE_ID])
        self.assertTrue(list(credentials)[0].startswith("sha256:"))
        self.assertEqual(len(list(credentials)[0]), len("sha256:") + 64)

    def test_a_wrong_code_is_refused_and_writes_nothing(self):
        service = self._service()
        port = free_port()
        server = ServiceThread(service, port)
        server.start()
        wrong = self.tmp / "wrong-token.txt"
        wrong.write_text("wrong-wrong-wrong-wrong-word\n")
        try:
            result = subprocess.run(
                [PY, "-B", str(ENROLL),
                 "--device-id", DEVICE_ID,
                 "--gateway", f"http://127.0.0.1:{port}",
                 "--out", str(self.out),
                 "--token-file", str(wrong)],
                capture_output=True, text=True, timeout=60)
        finally:
            server.stop()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.out.exists(), "a refused enrolment must write no files at all")


if __name__ == "__main__":
    unittest.main(verbosity=2)
