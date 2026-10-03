#!/usr/bin/env python3
"""The provisioned TLS credential blob: one implementation, used by the writer and the tests.

The device reads what scripts/provision-device-tls.py writes, so the format has exactly one
producer here and exactly one consumer in C (firmware/esp32/main/tls_credentials.c). This module is
imported by the provisioning script and by the host tests, so a change to the layout cannot be made
on one side only without a test noticing.

Layout (little-endian, fixed 132-byte header, then the payload):

    offset  size  field
    0       8     magic, b"HVTLS01\\x00"
    8       2     version (1)
    10      2     header length (132), so a format change is caught rather than misread
    12      4     total length of the blob, header included
    16      4     CA PEM length
    20      4     client certificate PEM length
    24      4     client key PEM length
    28      4     server port (informational, and what the report line prints)
    32      32    SHA-256 of the bridge's server certificate (DER)
    64      64    server host, NUL-padded (informational)
    128     4     CRC-32 (IEEE) over the payload
    132     ...   payload: CA PEM, client cert PEM, client key PEM

Each PEM length counts its own terminating NUL, and the payload holds no other NUL byte: mbedTLS
wants C strings, and a length that includes the terminator means the parser can hand each block over
without copying it into another buffer.
"""
import struct
import zlib

MAGIC = b'HVTLS01\x00'
VERSION = 1
HEADER_LEN = 132
SERVER_PIN_LEN = 32
SERVER_HOST_MAX = 64
REGION_BYTES = 8192

_HEADER = struct.Struct('<8sHHI III I 32s 64s I')
assert _HEADER.size == HEADER_LEN, _HEADER.size


def server_pin(cert_der: bytes) -> bytes:
    """The fingerprint a device pins: SHA-256 of the server certificate's DER form."""
    import hashlib
    return hashlib.sha256(cert_der).digest()


def build_blob(ca_pem: bytes, cert_pem: bytes, key_pem: bytes, pin: bytes, host: str,
               port: int, *, version: int = VERSION, magic: bytes = MAGIC,
               header_len: int = HEADER_LEN, crc: int | None = None, total_len: int | None = None,
               pad: bytes = b'') -> bytes:
    """Build a blob. Every argument that can be made wrong on purpose is exposed, so the tests can
    build the malformed blobs a corrupted or stale flash region would produce."""
    for name, pem in (('ca', ca_pem), ('cert', cert_pem), ('key', key_pem)):
        if not pem.endswith(b'\x00'):
            raise ValueError(f'{name} PEM must include its terminating NUL')
        if b'\x00' in pem[:-1]:
            raise ValueError(f'{name} PEM must not contain an embedded NUL')
    if len(pin) != SERVER_PIN_LEN:
        raise ValueError('server pin must be a 32-byte SHA-256 digest')
    host_bytes = host.encode('ascii')
    if len(host_bytes) >= SERVER_HOST_MAX:
        raise ValueError('server host must fit the header field with room for its NUL')
    payload = ca_pem + cert_pem + key_pem
    if HEADER_LEN + len(payload) > REGION_BYTES:
        raise ValueError('credential blob does not fit the reserved region')
    header = _HEADER.pack(magic, version, header_len,
                          HEADER_LEN + len(payload) if total_len is None else total_len,
                          len(ca_pem), len(cert_pem), len(key_pem), port,
                          pin, host_bytes.ljust(SERVER_HOST_MAX, b'\x00'),
                          zlib.crc32(payload) & 0xFFFFFFFF if crc is None else crc)
    if pad:
        return header + payload + pad
    return header + payload


def parse_blob(blob: bytes) -> dict:
    """The same validation the device performs, in Python. Used by tests to say what should be
    accepted, and by the provisioning script's readback check so both ends agree on intactness."""
    if len(blob) < HEADER_LEN:
        raise ValueError('blob is shorter than the header')
    magic, version, header_len, total_len, ca_len, cert_len, key_len, port, pin, host, crc = \
        _HEADER.unpack(blob[:HEADER_LEN])
    if magic != MAGIC:
        raise ValueError('bad magic')
    if version != VERSION:
        raise ValueError(f'unsupported version {version}')
    if header_len != HEADER_LEN:
        raise ValueError(f'header length {header_len} is not {HEADER_LEN}')
    if total_len != HEADER_LEN + ca_len + cert_len + key_len:
        raise ValueError('total length does not match the block lengths')
    if total_len > len(blob):
        raise ValueError('blob is shorter than its declared total length')
    payload = blob[HEADER_LEN:total_len]
    if zlib.crc32(payload) & 0xFFFFFFFF != crc:
        raise ValueError('CRC mismatch')
    texts = {}
    offset = 0
    for name, length in (('ca', ca_len), ('cert', cert_len), ('key', key_len)):
        block = payload[offset:offset + length]
        offset += length
        if not block.endswith(b'\x00') or b'\x00' in block[:-1]:
            raise ValueError(f'{name} is not a NUL-terminated string')
        if not block.startswith(b'-----BEGIN '):
            raise ValueError(f'{name} is not a PEM block')
        texts[name] = block[:-1].decode('ascii')
    host_text = host.split(b'\x00', 1)[0].decode('ascii', 'replace')
    return {'ca': texts['ca'], 'cert': texts['cert'], 'key': texts['key'],
            'pin': pin, 'host': host_text, 'port': port, 'total_len': total_len}
