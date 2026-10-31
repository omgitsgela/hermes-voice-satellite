"""Offline checks for the gateway's pairing endpoint.

No CA, no TLS handshake, no device: the enroller is stubbed and the clock is fake, so the security
behaviour is asserted directly rather than hoped for.

    python packages/installer/tests/run_pairing_server_tests.py
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for package in ("installer", "gateway"):
    sys.path.insert(0, str(ROOT / "packages" / package / "src"))

from hermes_gateway.pairing_server import (  # noqa: E402
    PairingConfig,
    PairingHandler,
    PairingRefused,
    PairingService,
)

WORDS = "amber lantern copper thrush meadow"
SERVER_CERT_PEM = b"-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----\n"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StubEnroller:
    def __init__(self, tls_dir: Path) -> None:
        self.tls_dir = tls_dir
        self.calls: list[str] = []

    def enroll(self, device_id: str) -> tuple[Path, Path]:
        self.calls.append(device_id)
        cert = self.tls_dir / f"{device_id}.crt"
        key = self.tls_dir / f"{device_id}.key"
        cert.write_text("DEVICE CERT")
        key.write_text("DEVICE KEY")
        return cert, key


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        tls = self.state / "registry-tls"
        tls.mkdir()
        (self.state / "enroll-token.txt").write_text(WORDS)
        (tls / "client-ca.crt").write_text("CA")
        (tls / "server.crt").write_bytes(SERVER_CERT_PEM)
        self.clock = FakeClock()
        self.enroller = StubEnroller(tls)
        self.service = PairingService(
            PairingConfig(state_dir=self.state, dial_address="tls://gateway.example:10702"),
            enroller=self.enroller,
            clock=self.clock,
        )

    def tearDown(self):
        self._tmp.cleanup()


class CorrectWords(Base):
    def test_the_payload_is_everything_a_workbench_needs(self):
        payload = self.service.pair(WORDS, "esp32-0011223344", "203.0.113.9")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["ca_pem"], "CA")
        self.assertEqual(payload["cert_pem"], "DEVICE CERT")
        self.assertEqual(payload["key_pem"], "DEVICE KEY")
        self.assertEqual(payload["dial_address"], "tls://gateway.example:10702")
        self.assertEqual(len(payload["server_pin"]), 64)

    def test_the_pin_is_the_sha256_of_the_certificate_der(self):
        import base64

        expected = hashlib.sha256(base64.b64decode("QUJD")).hexdigest()
        self.assertEqual(self.service.server_pin_hex(), expected)

    def test_case_and_spacing_are_forgiven(self):
        payload = self.service.pair("  Amber   Lantern copper THRUSH meadow ", "kitchen-1", "s")
        self.assertTrue(payload["ok"])

    def test_the_enroller_is_called_with_the_validated_id(self):
        self.service.pair(WORDS, "kitchen_2", "s")
        self.assertEqual(self.enroller.calls, ["kitchen_2"])


class ModuleStartup(unittest.TestCase):
    """The documented command must work, not just the import.

    The first version raised NameError on startup because the `__main__` guard sat above the
    function it calls, which no import-based test would ever notice.
    """

    def test_python_dash_m_reaches_the_check(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            tls = state / "registry-tls"
            tls.mkdir()
            (state / "enroll-token.txt").write_text(WORDS)
            (tls / "client-ca.crt").write_text("CA")
            (tls / "server.crt").write_bytes(SERVER_CERT_PEM)
            (tls / "server.key").write_text("KEY")
            env = {"PYTHONPATH": str(ROOT / "packages" / "gateway" / "src"), "PATH": "/usr/bin:/bin"}
            finished = subprocess.run(
                [__import__("sys").executable, "-m", "hermes_gateway.pairing_server",
                 "--state", str(state), "--dial", "tls://example:10702", "--check"],
                capture_output=True, text=True, env=env, timeout=60,
            )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("check only", finished.stdout)


class ServiceUnit(unittest.TestCase):
    """The endpoint has to be startable by something other than a person typing a command."""

    def config(self, **overrides):
        from hermes_gateway.unit import UnitConfig

        base = dict(
            python=Path("/usr/bin/python3"), state_dir=Path("/srv/hermes/state"),
            dial_address="tls://gateway.example:10702",
            working_directory=Path("/srv/hermes"), port=10704,
        )
        base.update(overrides)
        return UnitConfig(**base)

    def test_the_unit_runs_the_documented_command(self):
        from hermes_gateway.unit import render_unit

        text = render_unit(self.config())
        self.assertIn("-m hermes_gateway.pairing_server", text)
        self.assertIn("--state /srv/hermes/state", text)
        self.assertIn("--dial tls://gateway.example:10702", text)
        self.assertIn("--port 10704", text)
        self.assertIn("Restart=always", text)

    def test_a_path_with_a_space_is_quoted_for_systemd(self):
        from hermes_gateway.unit import render_unit

        text = render_unit(self.config(state_dir=Path("/srv/my state")))
        self.assertIn('--state "/srv/my state"', text)

    def test_an_empty_dial_address_is_refused(self):
        from hermes_gateway.unit import render_unit

        with self.assertRaises(ValueError):
            render_unit(self.config(dial_address=""))

    def test_writing_it_reports_how_to_enable_it(self):
        import tempfile

        from hermes_gateway.unit import enable_instructions, write_unit

        with tempfile.TemporaryDirectory() as tmp:
            target = write_unit(self.config(), Path(tmp) / "hermes-pairing.service")
            self.assertTrue(target.exists())
            self.assertIn("Hermes voice pairing endpoint", target.read_text())
            instructions = " ".join(enable_instructions(target))
            self.assertIn("systemctl --user enable --now hermes-pairing.service", instructions)


class Hardening(Base):
    def test_windows_reserved_names_are_refused_as_device_ids(self):
        for reserved in ("con", "aux", "nul", "com1", "lpt9"):
            with self.assertRaises(PairingRefused):
                self.service.pair(WORDS, reserved, "s")
        self.assertEqual(self.enroller.calls, [])

    def test_a_trailing_newline_is_not_a_device_id(self):
        with self.assertRaises(PairingRefused):
            self.service.pair(WORDS, "kitchen\n", "s")

    def test_the_source_map_is_bounded(self):
        for index in range(self.service.MAX_TRACKED_SOURCES + 50):
            self.clock.advance(1)
            with self.assertRaises(PairingRefused):
                self.service.pair("wrong", "kitchen-1", f"10.0.0.{index}")
        self.assertLessEqual(len(self.service._sources), self.service.MAX_TRACKED_SOURCES + 1)

    def test_enrolment_is_serialised(self):
        import threading
        import time

        peak = {"now": 0, "max": 0}
        guard = threading.Lock()
        original = self.enroller.enroll

        def slow_enroll(device_id):
            with guard:
                peak["now"] += 1
                peak["max"] = max(peak["max"], peak["now"])
            try:
                time.sleep(0.05)
                return original(device_id)
            finally:
                with guard:
                    peak["now"] -= 1

        self.enroller.enroll = slow_enroll
        threads = [
            threading.Thread(target=lambda i=i: self.service.pair(WORDS, f"device-{i}", f"src{i}"))
            for i in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak["max"], 1, "two enrolments ran at the same time")


class Refusals(Base):
    def test_wrong_words_are_refused_and_nothing_is_enrolled(self):
        with self.assertRaises(PairingRefused) as caught:
            self.service.pair("wrong words entirely", "kitchen-1", "s")
        self.assertIn("not correct", str(caught.exception))
        self.assertEqual(self.enroller.calls, [])

    def test_the_message_does_not_leak_how_close_a_guess_was(self):
        for attempt in ("amber lantern copper thrush m", "amber lantern copper thrush meado", ""):
            with self.assertRaises(PairingRefused) as caught:
                self.service.pair(attempt, "kitchen-1", "s")
            self.assertNotIn("amber", str(caught.exception).lower())

    def test_a_device_id_that_is_not_a_safe_filename_is_refused(self):
        for bad in ("../etc/passwd", "Kitchen", "with space", "-leading", "", "a" * 65):
            with self.assertRaises(PairingRefused):
                self.service.pair(WORDS, bad, "s")
        self.assertEqual(self.enroller.calls, [])

    def test_no_stored_words_is_an_answer_not_a_traceback(self):
        (self.state / "enroll-token.txt").unlink()
        with self.assertRaises(PairingRefused) as caught:
            self.service.pair(WORDS, "kitchen-1", "s")
        self.assertIn("no pairing words", str(caught.exception))


class RateLimiting(Base):
    def test_repeated_failures_lock_the_source_out(self):
        for _ in range(self.service.config.max_failures):
            self.clock.advance(1)
            with self.assertRaises(PairingRefused):
                self.service.pair("wrong", "kitchen-1", "203.0.113.9")
        self.clock.advance(1)
        with self.assertRaises(PairingRefused) as caught:
            self.service.pair(WORDS, "kitchen-1", "203.0.113.9")
        self.assertIn("too many attempts", str(caught.exception))

    def test_the_lock_expires(self):
        for _ in range(self.service.config.max_failures):
            self.clock.advance(1)
            with self.assertRaises(PairingRefused):
                self.service.pair("wrong", "kitchen-1", "203.0.113.9")
        self.clock.advance(self.service.config.lockout_seconds + 1)
        self.assertTrue(self.service.pair(WORDS, "kitchen-1", "203.0.113.9")["ok"])

    def test_one_source_cannot_lock_out_another(self):
        for _ in range(self.service.config.max_failures):
            self.clock.advance(1)
            with self.assertRaises(PairingRefused):
                self.service.pair("wrong", "kitchen-1", "203.0.113.9")
        self.clock.advance(1)
        self.assertTrue(self.service.pair(WORDS, "kitchen-2", "198.51.100.4")["ok"])

    def test_a_success_clears_the_failure_count(self):
        for _ in range(self.service.config.max_failures - 1):
            self.clock.advance(1)
            with self.assertRaises(PairingRefused):
                self.service.pair("wrong", "kitchen-1", "s")
        self.clock.advance(1)
        self.service.pair(WORDS, "kitchen-1", "s")
        for _ in range(self.service.config.max_failures - 1):
            self.clock.advance(1)
            with self.assertRaises(PairingRefused):
                self.service.pair("wrong", "kitchen-1", "s")
        self.clock.advance(1)
        # Not locked out: the success in the middle reset the counter.
        self.assertTrue(self.service.pair(WORDS, "kitchen-1", "s")["ok"])

    def test_attempts_too_close_together_are_slowed(self):
        with self.assertRaises(PairingRefused) as caught:
            self.service.pair("wrong", "kitchen-1", "s")
        self.assertIn("not correct", str(caught.exception))
        with self.assertRaises(PairingRefused) as caught:
            self.service.pair(WORDS, "kitchen-1", "s")
        self.assertIn("wait a moment", str(caught.exception))


class HttpLayer(Base):
    """The handler, over plain HTTP: TLS is the transport, not the logic under test."""

    @classmethod
    def setUpClass(cls):
        pass

    def setUp(self):
        super().setUp()
        handler = type("BoundPairing", (PairingHandler,), {"service": self.service})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def post(self, payload, content_type="application/json", raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base + "/pair", data=body,
            headers={"Content-Type": content_type}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_a_valid_pairing_returns_the_payload(self):
        status, payload = self.post({"words": WORDS, "device": "kitchen-1"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("cert_pem", payload)

    def test_wrong_words_are_401(self):
        status, payload = self.post({"words": "nope nope nope", "device": "kitchen-1"})
        self.assertEqual(status, 401)

    def test_lockout_is_429(self):
        for _ in range(self.service.config.max_failures):
            self.clock.advance(1)
            self.post({"words": "nope", "device": "kitchen-1"})
        self.clock.advance(1)
        status, _ = self.post({"words": WORDS, "device": "kitchen-1"})
        self.assertEqual(status, 429)

    def test_a_non_json_content_type_is_refused(self):
        status, _ = self.post({"words": WORDS, "device": "kitchen-1"}, content_type="text/plain")
        self.assertEqual(status, 415)

    def test_an_oversized_body_is_refused(self):
        status, _ = self.post(None, raw=b"{" + b"a" * 5000 + b"}")
        self.assertEqual(status, 400)

    def test_malformed_json_is_refused(self):
        status, _ = self.post(None, raw=b"{not json")
        self.assertEqual(status, 400)

    def test_get_is_not_a_way_in(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/pair", timeout=10)
        self.assertEqual(caught.exception.code, 405)

    def test_another_path_is_404(self):
        request = urllib.request.Request(
            self.base + "/enroll", data=b"{}", headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
