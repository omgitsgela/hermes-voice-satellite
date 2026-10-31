"""Pairing a workbench with a gateway.

The order matters more than anything else in this file:

1. connect and complete the TLS handshake,
2. derive the gateway's identity code from the certificate actually received,
3. show that code and **wait for the person to confirm it matches what they wrote down**,
4. only then send the pairing words.

The words guard the gateway's certificate authority, so they must never be sent to a host that has
not proved itself. Nothing secret moves before step 4, and the proof in step 2 is computed locally
from the peer certificate, not read from a response, so a wrong gateway cannot simply claim the
right value.
"""
from __future__ import annotations

import http.client
import json
import ssl
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .identity import identity_code, matches

PAIR_PATH = "/pair"


class PairingError(Exception):
    """Pairing failed for a reason worth showing the user."""


class PairingCancelled(Exception):
    """The person said the identity code did not match, or declined."""


@dataclass(frozen=True)
class GatewayRef:
    host: str
    port: int = 10702

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class PairedCredentials:
    """What the workbench needs to speak for itself to this gateway, and to provision devices."""

    gateway: GatewayRef
    identity_code: str
    ca_pem: str
    cert_pem: str
    key_pem: str
    server_pin: str
    dial_address: str

    def is_complete(self) -> bool:
        return all(
            [self.ca_pem.strip(), self.cert_pem.strip(), self.key_pem.strip(), self.server_pin.strip()]
        )


class Transport(Protocol):
    """The two things pairing needs from a connection, split out so it can be tested offline."""

    def peer_certificate_der(self) -> bytes: ...

    def post_json(self, path: str, payload: Mapping[str, Any]) -> dict: ...

    def close(self) -> None: ...


class TlsTransport:
    """A real connection.

    Certificate verification is deliberately off: at this point there is no CA on the workbench to
    verify against, which is the whole reason the identity code exists. The code is the
    authentication step, and the server pin returned by pairing is what subsequent connections
    check. Encryption is still on.
    """

    def __init__(self, ref: GatewayRef, timeout: float = 15.0) -> None:
        self._ref = ref
        self._timeout = timeout
        self._conn: http.client.HTTPSConnection | None = None

    def _connect(self) -> http.client.HTTPSConnection:
        if self._conn is not None:
            return self._conn
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(
            self._ref.host, self._ref.port, timeout=self._timeout, context=context
        )
        try:
            conn.connect()
        except OSError as exc:
            raise PairingError(f"could not reach {self._ref}: {exc}") from exc
        self._conn = conn
        return conn

    def peer_certificate_der(self) -> bytes:
        conn = self._connect()
        sock = conn.sock
        if sock is None:  # pragma: no cover - connect() raises instead
            raise PairingError(f"no connection to {self._ref}")
        der = sock.getpeercert(binary_form=True)
        if not der:
            raise PairingError(
                f"{self._ref} did not present a certificate, so its identity cannot be checked"
            )
        return der

    def post_json(self, path: str, payload: Mapping[str, Any]) -> dict:
        conn = self._connect()
        body = json.dumps(dict(payload)).encode()
        try:
            conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read()
        except OSError as exc:
            raise PairingError(f"pairing request to {self._ref} failed: {exc}") from exc
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise PairingError(f"{self._ref} answered with something that is not JSON") from None
        if response.status != 200 or not data.get("ok", False):
            detail = data.get("error") or f"HTTP {response.status}"
            raise PairingError(str(detail))
        return data

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def pair(
    ref: GatewayRef,
    words: str,
    device_name: str,
    *,
    confirm: Callable[[str], bool],
    expected_code: str | None = None,
    transport: Transport | None = None,
) -> PairedCredentials:
    """Pair with a gateway, proving it first.

    ``confirm`` is handed the identity code and returns True to continue. It is called exactly once
    and before any secret leaves the machine. If ``expected_code`` is given the comparison is made
    here instead, which is what a non-interactive caller (a script, a test) uses.
    """
    words = words.strip()
    device_name = device_name.strip()
    if not words:
        raise PairingError("the pairing words are empty")
    if not device_name:
        raise PairingError("a device name is required to enrol")

    own_transport = transport is None
    conn = transport or TlsTransport(ref)
    try:
        der = conn.peer_certificate_der()
        code = identity_code(der)

        if expected_code is not None:
            if not matches(der, expected_code):
                raise PairingCancelled(
                    f"this gateway's identity code is {code}, which does not match {expected_code}"
                )
        elif not confirm(code):
            raise PairingCancelled(
                f"identity code {code} was not confirmed, so nothing was sent to {ref}"
            )

        data = conn.post_json(PAIR_PATH, {"words": words, "device": device_name})
    finally:
        if own_transport:
            conn.close()

    credentials = PairedCredentials(
        gateway=ref,
        identity_code=code,
        ca_pem=str(data.get("ca_pem", "")),
        cert_pem=str(data.get("cert_pem", "")),
        key_pem=str(data.get("key_pem", "")),
        server_pin=str(data.get("server_pin", "")),
        dial_address=str(data.get("dial_address", f"tls://{ref.host}:{ref.port}")),
    )
    if not credentials.is_complete():
        raise PairingError(
            "the gateway accepted the words but did not return usable credentials; "
            "nothing has been written"
        )
    return credentials
