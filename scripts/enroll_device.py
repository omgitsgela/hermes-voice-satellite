#!/usr/bin/env python3
"""Enrol a fresh satellite with the gateway and receive its certificates.

A brand-new board has no secret of its own, so nothing distinguishes it from a stranger's board on
the same network. Enrolment therefore carries the OPERATOR's authority: a five-word setup code the
gateway printed once at install. The code itself never crosses the wire -- the request proves
knowledge of it with an HMAC over a one-time challenge -- and the answer comes back encrypted under
a key derived from the code, so only the code holder can open it and an impostor cannot forge one.

The wire protocol (the contract with the gateway's /enroll endpoints):

    GET  /enroll/challenge
      -> 200 {"nonce": "<64 hex>"}                     single use, 60 s TTL
         429 throttled

    POST /enroll
      Authorization: Bearer <proof>
      {"device_id": "..."}
      where proof = hex(HMAC-SHA256(key = token, msg = "enroll-v1:" + nonce))
      -> 200 {"ciphertext": "<b64>", "tag": "<b64>", "nonce": "<b64>"}
         401 bad or reused proof   400 bad device_id   409 already enrolled   429 throttled

    key       = HKDF-SHA256(ikm = token.encode(), salt = b"hermes-enroll-v1", info = b"bundle")
    plaintext = json({"client_ca": "<PEM>", "cert": "<PEM>", "key": "<PEM>"}).encode()

The setup code is NEVER an argument, never logged, and never printed: it arrives as a file path
(--token-file), from the saved file ~/.config/hermes-voice-satellite/enroll-token, or from a
no-echo prompt. The HMAC proof is never printed either -- a proof is cheap to brute force offline.

Usage:
    enroll_device.py --device-id esp32-68ee8f4b5988 --gateway http://192.0.2.10:8765 \\
        --out ~/.config/hermes-voice-satellite/certs \\
        --token-file ~/.config/hermes-voice-satellite/enroll-token
    enroll_device.py --device-id ... --gateway ... --out ... --endpoint tls://192.0.2.10:10702

With --endpoint the bridge's own server certificate is read from that TLS port and checked against
the client CA that arrived in the encrypted bundle before it is saved as `server.crt` -- the file
`provision-device-tls.py` pins into the board. The endpoint is the very address the board will
dial, so the certificate saved is the certificate the board will meet.
"""
import argparse
import base64
import hashlib
import hmac
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: The saved setup code, so it is typed once per machine and never again.
DEFAULT_TOKEN_FILE = Path.home() / '.config' / 'hermes-voice-satellite' / 'enroll-token'

#: The proof's context string: proof = hex(HMAC-SHA256(token, "enroll-v1:" + nonce)).
PROOF_CONTEXT = 'enroll-v1:'
#: The bundle key: HKDF-SHA256(ikm = token.encode(), salt = b"hermes-enroll-v1", info = b"bundle").
HKDF_SALT = b'hermes-enroll-v1'
HKDF_INFO = b'bundle'

DEVICE_ID_RE = re.compile(r'^[A-Za-z0-9._-]+$')
TOKEN_ARG_FLAGS = ('--token', '--token-value', '--enroll-token', '--setup-code', '--code')


class EnrolError(Exception):
    """A refusal the person running this can understand and act on."""


class AlreadyEnrolled(EnrolError):
    """HTTP 409: the name already has a certificate. Exit code 3, so a wrapper can offer to
    reissue instead of guessing at the cause of a generic failure."""
    """A failure that can be shown to the operator verbatim. No message here (or anywhere in this
    module) ever carries the setup code or the proof."""


class GatewayUnreachable(EnrolError):
    """The gateway did not answer at all (as opposed to answering with a refusal)."""


class RejectedResponse(EnrolError):
    """The gateway's answer could not be opened or trusted: AEAD authentication failed, or the
    shape is not the protocol's. Nothing from such an answer is ever written down."""


# ---------------------------------------------------------------- protocol primitives

def enrollment_proof(token: str, nonce: str) -> str:
    """The Bearer proof for one challenge: hex(HMAC-SHA256(token, "enroll-v1:" + nonce)).

    The nonce is the gateway's hex STRING, concatenated as text exactly as the protocol states.
    Never logged, never printed: this is the one value an attacker could brute force offline."""
    return hmac.new(token.encode('utf-8'), (PROOF_CONTEXT + nonce).encode('utf-8'),
                    hashlib.sha256).hexdigest()


def bundle_key(token: str) -> bytes:
    """HKDF-SHA256(ikm = token.encode(), salt = b"hermes-enroll-v1", info = b"bundle"), 32 bytes."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=HKDF_SALT,
                info=HKDF_INFO).derive(token.encode('utf-8'))


def open_bundle(token: str, response: dict) -> dict:
    """Open the gateway's AES-256-GCM response under the setup code and return the bundle.

    The response is {"ciphertext": b64, "tag": b64, "nonce": b64}. GCM authentication is the
    whole point: a wrong code or a tampered answer fails the tag check and NOTHING is trusted or
    written -- an impostor cannot forge a valid response, so there is no server identity to pin."""
    def field(name):
        # binascii.Error is a ValueError subclass, so this catches every decode failure.
        try:
            raw = base64.b64decode(response.get(name, ''), validate=True)
        except (TypeError, ValueError) as exc:
            raise RejectedResponse(f"the gateway's answer is not in the expected shape "
                                   f"({name} is not base64): {exc}") from None
        return raw

    nonce = field('nonce')
    ciphertext = field('ciphertext')
    tag = field('tag')
    if not 8 <= len(nonce) <= 128:
        raise RejectedResponse("the gateway's answer is not in the expected shape (nonce length)")
    if len(tag) != 16:
        raise RejectedResponse("the gateway's answer is not in the expected shape (tag length)")
    try:
        plaintext = AESGCM(bundle_key(token)).decrypt(nonce, ciphertext + tag, None)
    except InvalidTag:
        raise RejectedResponse('the gateway answer could not be opened: the setup code does not '
                               'match this gateway, or the answer was tampered with') from None
    except ValueError as exc:
        raise RejectedResponse("the gateway's answer is not in the expected shape: "
                               f'{exc}') from None
    try:
        bundle = json.loads(plaintext.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RejectedResponse("the opened answer is not the expected JSON bundle") from None
    if not isinstance(bundle, dict):
        raise RejectedResponse("the opened answer is not the expected JSON bundle")
    for name in ('client_ca', 'cert', 'key', 'server'):
        if name == 'server' and name not in bundle:
            continue   # an older gateway omits it; the caller captures it from --endpoint
        value = bundle.get(name)
        if not isinstance(value, str) or not value.startswith('-----BEGIN ') or \
                '-----END ' not in value:
            raise RejectedResponse(f"the opened answer has no usable {name} PEM block")
    return bundle


# ---------------------------------------------------------------- gateway exchange

def _http(url: str, *, data: bytes | None = None, headers: dict | None = None,
          timeout: float = 10.0):
    request = urllib.request.Request(url, data=data, headers=headers or {},
                                     method='POST' if data is not None else 'GET')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise GatewayUnreachable(f'could not reach the gateway at {url}: {exc}') from None


def fetch_challenge(gateway: str, timeout: float = 10.0) -> str:
    """GET /enroll/challenge -> the one-time nonce (a 64-hex string)."""
    status, body = _http(gateway + '/enroll/challenge', timeout=timeout)
    if status == 429:
        raise EnrolError('the gateway is busy (too many attempts); wait a minute and try again')
    if status != 200:
        raise EnrolError(f'the gateway refused the challenge (HTTP {status})')
    try:
        nonce = json.loads(body.decode('utf-8'))['nonce']
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise RejectedResponse("the gateway's challenge is not in the expected shape") from None
    if not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', nonce):
        raise RejectedResponse("the gateway's challenge is not in the expected shape (nonce)")
    return nonce


def post_enroll(gateway: str, proof: str, device_id: str, force: bool = False,
                timeout: float = 10.0) -> dict:
    """POST /enroll with the Bearer proof -> the encrypted bundle, or a clear refusal."""
    payload = json.dumps({'device_id': device_id, 'force': bool(force)}).encode('utf-8')
    status, body = _http(gateway + '/enroll', data=payload,
                         headers={'Authorization': 'Bearer ' + proof,
                                  'Content-Type': 'application/json'}, timeout=timeout)
    if status == 200:
        try:
            response = json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RejectedResponse("the gateway's answer is not in the expected shape") from None
        if not isinstance(response, dict):
            raise RejectedResponse("the gateway's answer is not in the expected shape")
        return response
    if status == 429:
        raise EnrolError('the gateway is busy (too many attempts); wait a minute and try again')
    if status == 401:
        raise EnrolError('the gateway did not accept the setup code (wrong code, or this '
                         "challenge was already used)")
    if status == 400:
        raise EnrolError(f'the gateway refused the device name {device_id!r}')
    if status == 409:
        # A distinct exception: this is the one refusal a person can act on by choosing,
        # so callers (install-esp32.sh) can tell it apart from a broken exchange.
        raise AlreadyEnrolled('this name is already enrolled at the gateway -- an '
                              'earlier attempt used it. Re-run with --force to issue '
                              'it a new certificate')
    raise EnrolError(f'the gateway answered HTTP {status}')


# ---------------------------------------------------------------- bridge certificate

def capture_server_cert(endpoint: str, client_ca_pem: str, timeout: float = 10.0) -> str:
    """The bridge's server certificate, read from the very endpoint the board will dial.

    The connection is verified against the client CA that arrived inside the AEAD-protected
    bundle, so the certificate saved is authenticated by that bundle: an impostor without a
    certificate the CA signed cannot complete the handshake and is refused here."""
    import socket
    import ssl

    body = endpoint.split('://', 1)[-1]
    host, _, port_text = body.rpartition(':')
    if not host or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise EnrolError(f'--endpoint is not tls://host:port: {endpoint}')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False          # the certificate's SANs are addresses, not names
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=client_ca_pem)
    try:
        with socket.create_connection((host, int(port_text)), timeout=timeout) as raw:
            with context.wrap_socket(raw) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise EnrolError(f'could not read the bridge certificate from {endpoint} '
                         f'(it must answer TLS, signed by the client CA): {exc}') from None
    if not der:
        raise EnrolError(f'the bridge at {endpoint} presented no certificate')
    return ssl.DER_cert_to_PEM_cert(der)


# ---------------------------------------------------------------- local output

def pem_to_der(pem: bytes) -> bytes:
    """The DER form of a PEM block -- loaded from provision-device-tls.py so the fingerprint here
    and the fingerprint the provisioner and the bridge compute can never drift apart."""
    spec = importlib.util.spec_from_file_location(
        'provision_device_tls', Path(__file__).resolve().parent / 'provision-device-tls.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.pem_to_der(pem)


def write_bundle(out_dir: Path, device_id: str, bundle: dict,
                 server_cert_pem: str | None = None) -> list:
    """Write client-ca.crt, <id>.crt, <id>.key (+ server.crt when captured) and a merged
    device-credentials.json into out_dir. The private key is 0600. Returns the files written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    def put(name, text, mode=0o644):
        path = out_dir / name
        path.write_text(text if text.endswith('\n') else text + '\n')
        path.chmod(mode)
        written.append(path)

    put('client-ca.crt', bundle['client_ca'])
    put(f'{device_id}.crt', bundle['cert'])
    put(f'{device_id}.key', bundle['key'], mode=0o600)
    if server_cert_pem is not None:
        put('server.crt', server_cert_pem)

    # The bridge's own record shape: fingerprint -> device id, computed exactly as the bridge
    # computes it. Existing entries for other boards are kept: the file grows, never shrinks.
    fingerprint = 'sha256:' + hashlib.sha256(
        pem_to_der(bundle['cert'].encode('utf-8'))).hexdigest()
    registry_path = out_dir / 'device-credentials.json'
    record = {}
    if registry_path.is_file():
        try:
            existing = json.loads(registry_path.read_text())
            if isinstance(existing, dict):
                record = existing
        except json.JSONDecodeError:
            raise EnrolError(f'{registry_path} exists but is not JSON; refusing to merge') from None
    record[fingerprint] = device_id
    registry_path.write_text(json.dumps(record, indent=2) + '\n')
    registry_path.chmod(0o600)
    written.append(registry_path)
    return written


# ---------------------------------------------------------------- setup code handling

def read_token(token_file: str | None) -> str:
    """The setup code, from a file path or a no-echo prompt -- never from argv."""
    if token_file:
        path = Path(token_file).expanduser()
        try:
            token = path.read_text().strip()
        except OSError as exc:
            raise EnrolError(f'cannot read the setup code file {path}: {exc}') from None
        if not token:
            raise EnrolError(f'{path} is empty')
        return token
    if DEFAULT_TOKEN_FILE.is_file():
        token = DEFAULT_TOKEN_FILE.read_text().strip()
        if token:
            return token
    import getpass
    token = getpass.getpass('Setup code (five words from your gateway): ').strip()
    if not token:
        raise EnrolError('no setup code given')
    return token


def _refuse_token_argv() -> None:
    """The setup code must never be an argument: argv is visible in the process list and lands in
    shell history. Refuse loudly -- but print the flag name only, never what followed it."""
    for arg in sys.argv[1:]:
        if arg in TOKEN_ARG_FLAGS or any(arg.startswith(flag + '=') for flag in TOKEN_ARG_FLAGS):
            raise SystemExit('enrol: never pass the setup code as an argument (it would sit in '
                             'the process list and the shell history); use --token-file PATH '
                             'or the saved file instead')


def main() -> int:
    _refuse_token_argv()
    # Line-buffer stdout so the progress lines interleave correctly with errors on stderr when
    # the output is a pipe (the guided installer captures both).
    reconfigure = getattr(sys.stdout, 'reconfigure', None)
    if reconfigure is not None:
        reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--force', action='store_true',
                        help='reissue the certificate when this name is already enrolled '
                             '(the gateway keeps the name and replaces its certificate)')
    parser.add_argument('--device-id', required=True,
                        help='the identity this board is enrolled under (the board reports it)')
    parser.add_argument('--gateway', required=True,
                        help="the gateway's enrol address, e.g. http://192.0.2.10:8765")
    parser.add_argument('--out', type=Path, required=True,
                        help='folder to write client-ca.crt, <id>.crt, <id>.key and '
                             'device-credentials.json into')
    parser.add_argument('--endpoint', help='tls://host:port the board will dial; when given, the '
                                           'bridge certificate is read from there (verified '
                                           'against the client CA) and saved as server.crt')
    parser.add_argument('--token-file', help='file holding the setup code; read, never printed. '
                                             'Without it the saved file '
                                             '~/.config/hermes-voice-satellite/enroll-token is '
                                             'used, then a no-echo prompt')
    arguments = parser.parse_args()

    if not DEVICE_ID_RE.match(arguments.device_id):
        raise SystemExit(f'enrol: --device-id {arguments.device_id!r} is not a usable device name')
    gateway = arguments.gateway.rstrip('/')
    if not gateway.startswith(('http://', 'https://')):
        raise SystemExit('enrol: --gateway must be an http(s):// address, e.g. '
                         'http://192.0.2.10:8765')

    token = read_token(arguments.token_file)
    try:
        print(f'enrol: asking {gateway} for a one-time challenge')
        nonce = fetch_challenge(gateway)
        proof = enrollment_proof(token, nonce)
        print(f'enrol: asking for the certificate of {arguments.device_id}')
        response = post_enroll(gateway, proof, arguments.device_id,
                               force=arguments.force)
        bundle = open_bundle(token, response)
        print('enrol: opened the encrypted answer (the setup code matched)')
        # The gateway now ships its own server certificate inside the sealed bundle, so one
        # connection is enough and nothing has to be read from a second TLS socket. Because the
        # whole bundle is AEAD-sealed under the setup code, an impostor cannot substitute a
        # server certificate either. Capture from --endpoint only against an older gateway that
        # does not send one.
        server_cert_pem = bundle.get('server')
        if server_cert_pem:
            print('enrol: the gateway sent its own server certificate in the sealed bundle')
        elif arguments.endpoint:
            server_cert_pem = capture_server_cert(arguments.endpoint, bundle['client_ca'])
            print(f'enrol: read the bridge certificate from {arguments.endpoint} '
                  '(verified against the client CA)')
        else:
            raise EnrolError('the gateway sent no server certificate, so the board would have '
                             'nothing to pin; pass --endpoint so it can be read from the '
                             'address the board will dial')
    finally:
        # The proof lives exactly as long as the exchange.
        del token

    written = write_bundle(arguments.out, arguments.device_id, bundle, server_cert_pem)
    print('enrol: wrote ' + ', '.join(str(path) for path in written))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except AlreadyEnrolled as error:
        print(f'enrol: {error}', file=sys.stderr)
        raise SystemExit(3)
    except EnrolError as error:
        print(f'enrol: {error}', file=sys.stderr)
        raise SystemExit(1)
