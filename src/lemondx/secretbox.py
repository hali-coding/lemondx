"""Encryption at rest for values lemondx must keep and later give back.

A stack secret -- a database password, say -- is unlike every other secret
lemondx handles: it cannot be hashed, because a launch has to hand the value
itself to a module, and a person may ask for it back. So it is stored
encrypted, and this is the cipher.

The backend is stdlib-only, and the stdlib has no block cipher. This is
encrypt-then-MAC from the primitives it does have: a keystream of SHA-256 over
(key, nonce, counter) -- SHA-256 in counter mode, a PRF as a stream cipher --
XORed with the plaintext, then HMAC-SHA256 over everything that is not secret.
The two keys are derived from one with HMAC, so encryption and authentication
never share a key. A fresh random nonce per value means no keystream is ever
used twice.

What it protects, honestly: a copy of the encrypted file on its own -- a
backup, a data directory copied to another machine, a file attached to a bug
report -- is useless without the key. What it does not protect: the key sits
beside it under ``auth/`` (0600), so whoever can read both, the service
account or root, can decrypt. That is the same boundary every other credential
under ``auth/`` has; no cipher can move it without a passphrase someone types
at every start.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

KEY_BYTES = 32
NONCE_BYTES = 16
TAG_BYTES = 32
VERSION = b"\x01"


class SecretBoxError(Exception):
    """A box that does not open: tampered with, truncated, or another node's key."""


def new_key():
    return secrets.token_bytes(KEY_BYTES)


def _subkeys(key):
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise SecretBoxError("The secrets key is not %d bytes." % KEY_BYTES)
    return (hmac.new(key, b"lemondx secretbox encrypt", hashlib.sha256).digest(),
            hmac.new(key, b"lemondx secretbox authenticate", hashlib.sha256).digest())


def _keystream(key, nonce, length):
    blocks = []
    for counter in range((length + 31) // 32):
        blocks.append(hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest())
    return b"".join(blocks)[:length]


def seal(key, text):
    """``text`` encrypted and authenticated, as printable ASCII for a JSON file."""
    encrypt, authenticate = _subkeys(key)
    plain = str(text).encode("utf-8")
    nonce = secrets.token_bytes(NONCE_BYTES)
    cipher = bytes(a ^ b for a, b in zip(plain, _keystream(encrypt, nonce, len(plain))))
    tag = hmac.new(authenticate, VERSION + nonce + cipher, hashlib.sha256).digest()
    return base64.b64encode(VERSION + nonce + cipher + tag).decode("ascii")


def open_(key, box):
    """The text sealed in ``box``, or raise if it was not sealed with ``key``."""
    encrypt, authenticate = _subkeys(key)
    try:
        raw = base64.b64decode(str(box).encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise SecretBoxError("Not a sealed value.") from None
    if len(raw) < 1 + NONCE_BYTES + TAG_BYTES or raw[:1] != VERSION:
        raise SecretBoxError("Not a sealed value.")
    nonce = raw[1:1 + NONCE_BYTES]
    cipher, tag = raw[1 + NONCE_BYTES:-TAG_BYTES], raw[-TAG_BYTES:]
    expected = hmac.new(authenticate, VERSION + nonce + cipher, hashlib.sha256).digest()
    # Checked before anything is decrypted: a forged box is refused, never read.
    if not hmac.compare_digest(tag, expected):
        raise SecretBoxError("A sealed value does not match this node's key.")
    plain = bytes(a ^ b for a, b in zip(cipher, _keystream(encrypt, nonce, len(cipher))))
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretBoxError("A sealed value did not decrypt to text.") from None
