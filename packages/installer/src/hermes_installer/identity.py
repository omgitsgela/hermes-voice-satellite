"""The gateway's short identity code.

A person is given one of these once, when they set up their gateway, and is asked to compare it
later when a config tool connects. That only means anything if the code is derived from something
the remote side cannot lie about, so it is computed from the gateway's TLS certificate: the tool
derives it locally from the certificate it actually received, never from a field in a response.

Shape: eight characters in two groups of four, Crockford base32, for example ``XK7F-9Q2M``.

Crockford base32 is used because it removes I, L, O and U, the characters people misread as 1, 1, 0
and V. The fourty bits come from the first five bytes of the certificate's SHA-256, which is a whole
number of base32 characters with no padding to argue about.

Why forty bits: it has to be short enough to write on a card and long enough that nobody can grind
certificates until one matches. A determined attacker would need on the order of 2**40 certificates,
and every attempt is a certificate they must also get the user to accept.
"""
from __future__ import annotations

import hashlib
import hmac

#: Crockford's alphabet: no I, L, O, U, so there is no 1/0 confusion when a person reads it aloud.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Characters people might type when a code was written by hand.
_CONFUSABLE = {"I": "1", "L": "1", "O": "0", "U": "V"}

CODE_CHARS = 8
GROUP_SIZE = 4


def _encode(raw: bytes) -> str:
    """Base32 with Crockford's alphabet, MSB first, no padding. Only whole groups are used."""
    bits = "".join(f"{byte:08b}" for byte in raw)
    out = []
    for start in range(0, len(bits) - len(bits) % 5, 5):
        out.append(ALPHABET[int(bits[start:start + 5], 2)])
    return "".join(out)


def format_code(code: str) -> str:
    """Group a bare code for display: ``XK7F9Q2M`` becomes ``XK7F-9Q2M``."""
    bare = normalise_code(code)
    groups = [bare[i:i + GROUP_SIZE] for i in range(0, len(bare), GROUP_SIZE)]
    return "-".join(groups)


def normalise_code(code: str) -> str:
    """Accept what a person types: lowercase, hyphens, spaces, and the confusable letters."""
    cleaned = "".join(ch for ch in code.upper() if not ch.isspace() and ch != "-")
    cleaned = "".join(_CONFUSABLE.get(ch, ch) for ch in cleaned)
    bad = sorted({ch for ch in cleaned if ch not in ALPHABET})
    if bad:
        raise ValueError(f"not a valid identity code: contains {''.join(bad)!r}")
    return cleaned


def identity_code(certificate_der: bytes) -> str:
    """The display form of the code for a certificate, e.g. ``XK7F-9Q2M``."""
    if not certificate_der:
        raise ValueError("no certificate to derive an identity code from")
    digest = hashlib.sha256(certificate_der).digest()
    return format_code(_encode(digest[:5])[:CODE_CHARS])


def matches(certificate_der: bytes, expected: str) -> bool:
    """Constant-time comparison of a certificate against a code a person wrote down.

    Constant time because this is a secret-derived value being compared against user input, and a
    timing side channel on a code check is free to close.
    """
    try:
        want = normalise_code(expected)
    except ValueError:
        return False
    have = normalise_code(identity_code(certificate_der))
    return hmac.compare_digest(have, want)
