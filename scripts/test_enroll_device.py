#!/usr/bin/env python3
"""Offline checks for scripts/enroll_device.py -- the plug-and-play enrolment client.

Run with the project's interpreter (no pytest here):

    ./.venv/bin/python -B scripts/test_enroll_device.py

The client speaks the /enroll wire protocol to the gateway on behalf of a brand-new board whose
only credential is the operator's five-word setup code. What is pinned here:

  1  the proof is exactly hex(HMAC-SHA256(token, "enroll-v1:" + nonce)) for a known token/nonce
     (a fixed vector, so both sides of the wire cannot drift), and the bundle key is exactly
     HKDF-SHA256(ikm = token, salt = b"hermes-enroll-v1", info = b"bundle")
  2  a full exchange against a stub gateway that implements the written protocol produces the
     expected files: client-ca.crt, <id>.crt, <id>.key and a device-credentials.json whose
     fingerprint entry is the one the Gateway's lookup builds
  3  a bad server response (wrong key, tampered tag) is REJECTED by the AEAD check and nothing
     is written -- an unauthenticated answer is never trusted
  4  the setup code never appears in any output, and passing it as an argument is refused
     (argv lands in the process list and the shell history)
  5  gateway refusals (401/409/429) and a bad challenge are reported as clear errors
  6  with --endpoint the Gateway certificate is read from that TLS port, checked against the
     client CA from the bundle, and saved as server.crt -- and a certificate the CA did not
     sign is refused

Everything runs against local stub servers on 127.0.0.1; no real gateway is contacted.
"""
import base64
import datetime
import hashlib
import hmac
import importlib.util
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
ENROLL = ROOT / 'scripts' / 'enroll_device.py'

# A test-only sentinel standing in for the gateway's five-word code. It is not a real token.
TOKEN = 'zebra-mango-tulip-quartz-ivory'
NONCE = 'ab' * 32
# The vector for TOKEN/NONCE, recomputed independently with stdlib hmac in the test below:
PROOF_VECTOR = '19a0004064e4aa2f7de7f5ceaa843a7078ae2f29f08605c7abad63aa398f6552'
KEY_VECTOR = 'b94c5c0f310b27e237480128f2d4e1290a645e8e857c6c653309720bab23abf0'


def load_enroll():
    """Load scripts/enroll_device.py as a module (its name is importable, but loading by path
    keeps the test independent of sys.path)."""
    spec = importlib.util.spec_from_file_location('enroll_device', ENROLL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = load_enroll()


def make_cert(common_name, ca=None, ca_key=None, for_server=False):
    """One certificate (and key) PEM pair. With ca/ca_key the cert is signed by that CA; without
    them it is a self-signed CA."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (x509.CertificateBuilder()
               .subject_name(name).issuer_name(ca.subject if ca is not None else name)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(days=1))
               .not_valid_after(now + datetime.timedelta(days=1)))
    if ca is None:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True)
    if for_server:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        builder = builder.add_extension(
            x509.KeyUsage(digital_signature=True, key_encipherment=False,
                          content_commitment=False, data_encipherment=False,
                          key_agreement=False, key_cert_sign=False, crl_sign=False,
                          encipher_only=False, decipher_only=False), critical=True)
    cert = builder.sign(ca_key if ca_key is not None else key, hashes.SHA256())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode('ascii')
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode('ascii')
    return cert_pem, key_pem


CA_PEM, CA_KEY_PEM = make_cert('test-enrol-ca')
CA_CERT = x509.load_pem_x509_certificate(CA_PEM.encode('ascii'))
CA_KEY = serialization.load_pem_private_key(CA_KEY_PEM.encode('ascii'), password=None)
DEVICE_CERT_PEM, DEVICE_KEY_PEM = make_cert('esp32-enroll-test')
SERVER_PEM, SERVER_KEY_PEM = make_cert('test-bridge', CA_CERT, CA_KEY, for_server=True)
ROGUE_PEM, ROGUE_KEY_PEM = make_cert('rogue-bridge', None, None, for_server=True)

BUNDLE = {'client_ca': CA_PEM, 'cert': DEVICE_CERT_PEM, 'key': DEVICE_KEY_PEM,
          'server': SERVER_PEM}
#: An older gateway's payload, which carried no server certificate: exercises the capture
#: fallback that reads it from the endpoint the board will dial.
BUNDLE_WITHOUT_SERVER = {k: v for k, v in BUNDLE.items() if k != 'server'}


class StubGatewayHandler(BaseHTTPRequestHandler):
    """The gateway half of the written protocol, for the client to talk to. Class attributes are
    the test knobs; seen_* record what arrived so a test can check the wire shape."""

    token = TOKEN
    nonce = NONCE
    bundle = BUNDLE         # the sealed payload; a test can send a smaller one (older gateway)
    wrong_key = False       # seal the bundle under a DIFFERENT code (the AEAD must refuse it)
    get_status = 200
    post_status = 200
    challenge = None        # override the challenge JSON wholesale when set
    seen_authorization = ''
    seen_device_id = None

    def log_message(self, format, *args):
        pass

    def _send(self, status, payload=None):
        body = b'' if payload is None else json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path != '/enroll/challenge':
            return self._send(404)
        if self.get_status != 200:
            return self._send(self.get_status)
        if self.challenge is not None:
            return self._send(200, self.challenge)
        return self._send(200, {'nonce': self.nonce})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        request = json.loads(self.rfile.read(length) or b'{}')
        StubGatewayHandler.seen_authorization = self.headers.get('Authorization', '')
        StubGatewayHandler.seen_device_id = request.get('device_id')
        if self.post_status != 200:
            return self._send(self.post_status)
        expected = hmac.new(self.token.encode('utf-8'),
                            b'enroll-v1:' + self.nonce.encode('utf-8'),
                            hashlib.sha256).hexdigest()
        if self.seen_authorization != 'Bearer ' + expected:
            return self._send(401)
        sealing_token = 'tampered-tampered-tampered' if self.wrong_key else self.token
        nonce_bytes = os.urandom(24)
        sealed = AESGCM(MOD.bundle_key(sealing_token)).encrypt(
            nonce_bytes, json.dumps(self.bundle).encode('utf-8'), None)
        return self._send(200, {
            'ciphertext': base64.b64encode(sealed[:-16]).decode('ascii'),
            'tag': base64.b64encode(sealed[-16:]).decode('ascii'),
            'nonce': base64.b64encode(nonce_bytes).decode('ascii')})


class StubGateway:
    """A stub gateway on 127.0.0.1:0, running in a thread. Re-configurable per test."""

    def __init__(self, **knobs):
        StubGatewayHandler.token = TOKEN
        StubGatewayHandler.nonce = NONCE
        StubGatewayHandler.bundle = BUNDLE
        StubGatewayHandler.wrong_key = False
        StubGatewayHandler.get_status = 200
        StubGatewayHandler.post_status = 200
        StubGatewayHandler.challenge = None
        StubGatewayHandler.seen_authorization = ''
        StubGatewayHandler.seen_device_id = None
        for name, value in knobs.items():
            setattr(StubGatewayHandler, name, value)
        self.server = HTTPServer(('127.0.0.1', 0), StubGatewayHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def seen_authorization(self):
        return StubGatewayHandler.seen_authorization

    @property
    def seen_device_id(self):
        return StubGatewayHandler.seen_device_id

    @property
    def url(self):
        host, port = self.server.server_address[:2]
        return f'http://{host}:{port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class StubTLSServer:
    """A TLS listener that completes a handshake and closes -- the Gateway certificate the
    client captures from --endpoint."""

    def __init__(self, cert_pem, key_pem):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tmp = tempfile.mkdtemp(prefix='enrol-tls-')
        cert_path = Path(tmp) / 's.crt'
        key_path = Path(tmp) / 's.key'
        cert_path.write_text(cert_pem)
        key_path.write_text(key_pem)
        context.load_cert_chain(str(cert_path), str(key_path))
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen(4)
        self.port = self.socket.getsockname()[1]
        self.context = context
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.socket.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(conn, server_side=True):
                    pass   # handshake done; the client reads the certificate and disconnects
            except (OSError, ssl.SSLError):
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self):
        try:
            self.socket.close()
        except OSError:
            pass


class ProtocolVectorTests(unittest.TestCase):
    """The proof and the bundle key are the wire contract: pinned as literals so the installer
    and the gateway cannot drift apart silently."""

    def test_proof_for_a_known_token_and_nonce(self):
        expected = hmac.new(TOKEN.encode(), (b'enroll-v1:' + NONCE.encode()), hashlib.sha256)
        self.assertEqual(expected.hexdigest(), PROOF_VECTOR)   # the vector itself is honest
        self.assertEqual(MOD.enrollment_proof(TOKEN, NONCE), PROOF_VECTOR)

    def test_bundle_key_for_a_known_token(self):
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        reference = HKDF(algorithm=hashes.SHA256(), length=32,
                         salt=b'hermes-enroll-v1', info=b'bundle').derive(TOKEN.encode())
        self.assertEqual(reference.hex(), KEY_VECTOR)
        self.assertEqual(MOD.bundle_key(TOKEN).hex(), KEY_VECTOR)

    def test_open_bundle_rejects_a_wrong_key_answer(self):
        """The AEAD is the only authentication of the gateway: an answer sealed under another
        key must be refused outright, never partially trusted."""
        nonce_bytes = os.urandom(24)
        sealed = AESGCM(MOD.bundle_key('another-another-another-key')).encrypt(
            nonce_bytes, json.dumps(BUNDLE).encode('utf-8'), None)
        response = {'ciphertext': base64.b64encode(sealed[:-16]).decode(),
                    'tag': base64.b64encode(sealed[-16:]).decode(),
                    'nonce': base64.b64encode(nonce_bytes).decode()}
        with self.assertRaises(MOD.RejectedResponse):
            MOD.open_bundle(TOKEN, response)

    def test_open_bundle_rejects_a_tampered_tag(self):
        nonce_bytes = os.urandom(24)
        sealed = AESGCM(MOD.bundle_key(TOKEN)).encrypt(
            nonce_bytes, json.dumps(BUNDLE).encode('utf-8'), None)
        tag = bytearray(sealed[-16:])
        tag[0] ^= 0xFF
        response = {'ciphertext': base64.b64encode(sealed[:-16]).decode(),
                    'tag': base64.b64encode(bytes(tag)).decode(),
                    'nonce': base64.b64encode(nonce_bytes).decode()}
        with self.assertRaises(MOD.RejectedResponse):
            MOD.open_bundle(TOKEN, response)

    def test_open_bundle_rejects_a_malformed_shape(self):
        for response in ({}, {'ciphertext': 'x', 'tag': 'y', 'nonce': 'z'},
                         {'ciphertext': '!!!', 'tag': '!!!', 'nonce': '!!!'}):
            with self.subTest(response=response):
                with self.assertRaises(MOD.RejectedResponse):
                    MOD.open_bundle(TOKEN, response)


class ExchangeTests(unittest.TestCase):
    """The whole exchange against a stub gateway that implements the written protocol."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='enrol-test-'))
        self.out = self.tmp / 'certs'
        self.token_file = self.tmp / 'token.txt'
        self.token_file.write_text(TOKEN + '\n')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, gateway_url, *extra):
        return subprocess.run(
            [sys.executable, str(ENROLL), '--device-id', 'esp32-enroll-test',
             '--gateway', gateway_url, '--out', str(self.out),
             '--token-file', str(self.token_file), *extra],
            capture_output=True, text=True)

    def test_full_exchange_writes_the_expected_files(self):
        gateway = StubGateway()
        try:
            result = self.run_cli(gateway.url)
        finally:
            gateway.close()
        self.assertEqual(result.returncode, 0, result.stderr)
        # The four files the protocol promises.
        self.assertEqual((self.out / 'client-ca.crt').read_text().strip(),
                         CA_PEM.strip())
        self.assertEqual((self.out / 'esp32-enroll-test.crt').read_text().strip(),
                         DEVICE_CERT_PEM.strip())
        self.assertEqual((self.out / 'esp32-enroll-test.key').read_text().strip(),
                         DEVICE_KEY_PEM.strip())
        registry = json.loads((self.out / 'device-credentials.json').read_text())
        fingerprint = 'sha256:' + hashlib.sha256(
            base64.b64decode(''.join(DEVICE_CERT_PEM.splitlines()[1:-1]))).hexdigest()
        self.assertEqual(registry, {fingerprint: 'esp32-enroll-test'})
        # The private key is private.
        self.assertEqual((self.out / 'esp32-enroll-test.key').stat().st_mode & 0o777, 0o600)
        # The wire shape: the proof is the vector, and the code itself never travelled.
        self.assertEqual(gateway.seen_authorization, 'Bearer ' + PROOF_VECTOR)
        self.assertEqual(gateway.seen_device_id, 'esp32-enroll-test')
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertNotIn(PROOF_VECTOR, result.stdout + result.stderr)

    def test_the_never_emitted_token_rule_holds_on_every_surface(self):
        gateway = StubGateway()
        try:
            result = self.run_cli(gateway.url)
        finally:
            gateway.close()
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(TOKEN, combined)
        for word in TOKEN.split('-'):
            self.assertNotIn(f'Bearer {word}', combined)
        self.assertNotIn(PROOF_VECTOR, combined)

    def test_a_wrong_key_answer_writes_nothing(self):
        """A response the AEAD refuses must leave no files behind: an answer that cannot be
        authenticated is garbage, not half-trusted material."""
        gateway = StubGateway(wrong_key=True)
        try:
            result = self.run_cli(gateway.url)
        finally:
            gateway.close()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not be opened', result.stderr)
        self.assertFalse(self.out.exists() and any(self.out.iterdir()),
                        f'wrote files from an unauthenticated answer: {list(self.out.glob("*"))}')
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_the_token_is_refused_as_an_argument(self):
        """argv is visible in the process list and lands in shell history: a --token argument is
        refused loudly, and what followed it is never echoed back."""
        result = subprocess.run(
            [sys.executable, str(ENROLL), '--device-id', 'esp32-enroll-test',
             '--gateway', 'http://127.0.0.1:1', '--out', str(self.out),
             '--token', TOKEN],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('never pass the setup code as an argument', result.stdout + result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_gateway_refusals_are_reported_as_clear_errors(self):
        for status, needle in ((401, 'did not accept the setup code'),
                               (409, 'already enrolled'),
                               (429, 'busy')):
            with self.subTest(status=status):
                gateway = StubGateway(post_status=status)
                try:
                    result = self.run_cli(gateway.url)
                finally:
                    gateway.close()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(needle, result.stderr)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_an_unreachable_gateway_is_reported_as_unreachable(self):
        result = self.run_cli('http://127.0.0.1:1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not reach the gateway', result.stderr)

    def test_a_bad_challenge_is_refused(self):
        gateway = StubGateway(challenge={'nonce': 'not-hex'})
        try:
            result = self.run_cli(gateway.url)
        finally:
            gateway.close()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('challenge', result.stderr)

    def test_the_saved_token_file_is_used_without_a_prompt(self):
        """~/.config/hermes-voice-satellite/enroll-token is the typed-once path; with it present
        the client needs no --token-file and never prompts."""
        home = self.tmp / 'home'
        (home / '.config' / 'hermes-voice-satellite').mkdir(parents=True)
        (home / '.config' / 'hermes-voice-satellite' / 'enroll-token').write_text(TOKEN + '\n')
        gateway = StubGateway()
        try:
            result = subprocess.run(
                [sys.executable, str(ENROLL), '--device-id', 'esp32-enroll-test',
                 '--gateway', gateway.url, '--out', str(self.out)],
                capture_output=True, text=True,
                env={**os.environ, 'HOME': str(home)})
        finally:
            gateway.close()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.out / 'esp32-enroll-test.crt').is_file())
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_a_registry_file_grows_never_shrinks(self):
        fingerprint_old = 'sha256:' + '0' * 64
        self.out.mkdir(parents=True)
        (self.out / 'device-credentials.json').write_text(
            json.dumps({fingerprint_old: 'older-board'}))
        gateway = StubGateway()
        try:
            result = self.run_cli(gateway.url)
        finally:
            gateway.close()
        self.assertEqual(result.returncode, 0, result.stderr)
        registry = json.loads((self.out / 'device-credentials.json').read_text())
        self.assertEqual(registry.get(fingerprint_old), 'older-board')
        self.assertIn('esp32-enroll-test', registry.values())


class ServerCertCaptureTests(unittest.TestCase):
    """--endpoint: the Gateway certificate pinned into the board comes from the very address the
    board will dial, and is checked against the client CA that arrived inside the bundle."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='enrol-cert-'))
        self.out = self.tmp / 'certs'
        self.token_file = self.tmp / 'token.txt'
        self.token_file.write_text(TOKEN + '\n')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, gateway_url, endpoint):
        return subprocess.run(
            [sys.executable, str(ENROLL), '--device-id', 'esp32-enroll-test',
             '--gateway', gateway_url, '--endpoint', endpoint, '--out', str(self.out),
             '--token-file', str(self.token_file)],
            capture_output=True, text=True)

    def test_the_bridge_certificate_is_captured_and_verified(self):
        # An OLDER gateway sends no server certificate in the sealed bundle, so the client must
        # fall back to reading it from the endpoint the board will dial.
        gateway = StubGateway(bundle=BUNDLE_WITHOUT_SERVER)
        tls = StubTLSServer(SERVER_PEM, SERVER_KEY_PEM)
        try:
            result = self.run_cli(gateway.url, f'tls://127.0.0.1:{tls.port}')
        finally:
            gateway.close()
            tls.close()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.out / 'server.crt').read_text().strip(), SERVER_PEM.strip())
        self.assertIn('verified against the client CA', result.stdout)

    def test_a_certificate_the_ca_did_not_sign_is_refused(self):
        gateway = StubGateway(bundle=BUNDLE_WITHOUT_SERVER)
        tls = StubTLSServer(ROGUE_PEM, ROGUE_KEY_PEM)
        try:
            result = self.run_cli(gateway.url, f'tls://127.0.0.1:{tls.port}')
        finally:
            gateway.close()
            tls.close()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not read the Gateway certificate', result.stderr)
        self.assertFalse((self.out / 'server.crt').exists())


class WriteShapeTests(unittest.TestCase):
    """write_bundle's on-disk shape, checked directly (no server involved)."""

    def test_key_is_private_and_the_registry_maps_the_real_fingerprint(self):
        out = Path(tempfile.mkdtemp(prefix='enrol-write-')) / 'certs'
        try:
            written = MOD.write_bundle(out, 'esp32-enroll-test', dict(BUNDLE))
            self.assertEqual({path.name for path in written},
                             {'client-ca.crt', 'esp32-enroll-test.crt',
                              'esp32-enroll-test.key', 'device-credentials.json'})
            self.assertEqual(out.joinpath('esp32-enroll-test.key').stat().st_mode & 0o777, 0o600)
            self.assertEqual(out.joinpath('device-credentials.json').stat().st_mode & 0o777,
                             0o600)
            # The fingerprint is the DER hash the Gateway looks up (pem_to_der, one producer).
            der = MOD.pem_to_der(DEVICE_CERT_PEM.encode('ascii'))
            registry = json.loads((out / 'device-credentials.json').read_text())
            self.assertEqual(registry,
                             {'sha256:' + hashlib.sha256(der).hexdigest(): 'esp32-enroll-test'})
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
