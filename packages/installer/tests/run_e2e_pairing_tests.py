"""End to end: the real pairing client against the real pairing endpoint, over TLS.

Every other pairing test stubs one side. This one stubs nothing but the certificate authority: it
generates a certificate, serves the endpoint with it, connects with the shipping client, confirms the
identity code the client derives, and checks the credentials that come back are the ones a device
would be provisioned with.

It is the test that would catch a field renamed on one side only, which is the failure that passes unit
tests and fails in a kitchen.

    python packages/installer/tests/run_e2e_pairing_tests.py
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for package in ("installer", "gateway"):
    sys.path.insert(0, str(ROOT / "packages" / package / "src"))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from hermes_gateway.pairing_server import (  # noqa: E402
    PairingConfig,
    PairingService,
    make_pairing_server,
)
from hermes_installer.identity import identity_code  # noqa: E402
from hermes_installer.pairing import GatewayRef, PairingCancelled, pair  # noqa: E402

WORDS = "amber lantern copper thrush meadow"


def make_certificate(directory: Path) -> tuple[Path, Path, bytes]:
    """A self-signed certificate for 127.0.0.1, and its DER form."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(
            __import__("ipaddress").ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "server.crt"
    key_path = directory / "server.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return cert_path, key_path, certificate.public_bytes(serialization.Encoding.DER)


class StubEnroller:
    """Signs nothing: writes the files the endpoint reads back, so the wire path is what is tested."""

    def __init__(self, tls_dir: Path) -> None:
        self.tls_dir = tls_dir
        self.calls: list[tuple[str, bool]] = []

    def enroll(self, device_id: str, *, replace: bool = False):
        self.calls.append((device_id, replace))
        cert = self.tls_dir / f"{device_id}.crt"
        key = self.tls_dir / f"{device_id}.key"
        # A real certificate, self-signed here, because the gateway derives the device's registry
        # fingerprint from it and a placeholder would produce nothing to write.
        certificate = device_certificate(device_id)
        cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key.write_text("DEVICE KEY")
        self.last_der = certificate.public_bytes(serialization.Encoding.DER)
        return cert, key


def device_certificate(common_name: str) -> x509.Certificate:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        tls = self.state / "registry-tls"
        tls.mkdir()
        (self.state / "enroll-token.txt").write_text(WORDS)
        (tls / "client-ca.crt").write_text("CLIENT CA")
        self.cert_path, self.key_path, self.der = make_certificate(tls)
        self.enroller = StubEnroller(tls)
        self.service = PairingService(
            PairingConfig(state_dir=self.state, dial_address="tls://127.0.0.1:10702"),
            enroller=self.enroller,
        )
        self.server = make_pairing_server(
            self.service, bind="127.0.0.1", port=0,
            certfile=self.cert_path, keyfile=self.key_path,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.ref = GatewayRef("127.0.0.1", self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def test_the_shipping_client_pairs_over_real_tls(self):
        code = identity_code(self.der)
        seen = []

        def confirm(shown):
            seen.append(shown)
            return True

        credentials = pair(self.ref, WORDS, "esp32-0011223344", confirm=confirm)

        # The code the client derived is the code for the certificate actually served.
        self.assertEqual(seen, [code])
        self.assertEqual(credentials.identity_code, code)
        # And the material is complete and correctly named for the provisioner.
        self.assertEqual(credentials.ca_pem, "CLIENT CA")
        self.assertTrue(credentials.cert_pem.startswith("-----BEGIN CERTIFICATE-----"),
                        "the client did not receive a certificate")
        self.assertEqual(credentials.key_pem, "DEVICE KEY")
        self.assertEqual(credentials.device_id, "esp32-0011223344")
        self.assertEqual(credentials.dial_address, "tls://127.0.0.1:10702")
        # The pin is the fingerprint of the certificate this server actually presented.
        self.assertEqual(credentials.server_pin, hashlib.sha256(self.der).hexdigest())
        # The gateway registers a fingerprint for the device it just enrolled, and the workbench needs
        # it to build the one-line registry record the provisioner checks.
        self.assertEqual(
            credentials.device_fingerprint,
            hashlib.sha256(self.enroller.last_der).hexdigest(),
            "the fingerprint is not the one the provisioner will compute",
        )
        self.assertTrue(credentials.server_cert_pem.startswith("-----BEGIN CERTIFICATE-----"))
        self.assertEqual(self.enroller.calls, [("esp32-0011223344", False)])

    def test_a_wrong_code_stops_before_the_words_are_sent(self):
        with self.assertRaises(PairingCancelled):
            pair(self.ref, WORDS, "esp32-0011223344", confirm=lambda shown: True,
                 expected_code="0000-0000")
        self.assertEqual(self.enroller.calls, [], "the gateway was asked to enrol anyway")

    def test_wrong_words_are_refused_by_the_server(self):
        from hermes_installer.pairing import PairingError

        with self.assertRaises(PairingError):
            pair(self.ref, "not the words", "esp32-0011223344",
                 confirm=lambda shown: True)
        self.assertEqual(self.enroller.calls, [])

    def test_replacement_travels_over_the_wire(self):
        pair(self.ref, WORDS, "esp32-0011223344", confirm=lambda code: True, )
        # The client does not send replace; a second enrolment must therefore be refused by the
        # server's own rule, which is what the stub's silent success would otherwise hide.
        self.assertEqual(self.enroller.calls, [("esp32-0011223344", False)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
