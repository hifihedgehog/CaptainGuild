"""The master key and what is derived from it.

The master key lives in the environment. Database keys and session secrets are derived from it with
HKDF-SHA256 (RFC 5869) and never stored.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

MASTER_KEY_BYTES = 32


def hkdf_sha256(ikm: bytes, *, salt: bytes = b"", info: bytes = b"", length: int = 32) -> bytes:
    """HKDF-Extract then HKDF-Expand, as defined in RFC 5869 sections 2.2 and 2.3."""
    if length > 255 * 32:
        raise ValueError("HKDF output is limited to 255 hash blocks")
    prk = hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    output, block = b"", b""
    counter = 1
    while len(output) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        output += block
        counter += 1
    return output[:length]


def decode_master_key(value: str) -> bytes:
    """Read the base64 master key from the environment and refuse anything too short to be safe."""
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except ValueError as error:
        raise ValueError("MASTER_KEY is not valid base64") from error
    if len(key) < MASTER_KEY_BYTES:
        raise ValueError(f"MASTER_KEY must hold at least {MASTER_KEY_BYTES} bytes")
    return key


def new_master_key() -> str:
    return base64.b64encode(secrets.token_bytes(MASTER_KEY_BYTES)).decode()


class Keyring:
    def __init__(self, master: bytes) -> None:
        self._master = master

    def _derive(self, purpose: str) -> bytes:
        return hkdf_sha256(self._master, info=purpose.encode())

    def database_key(self, name: str = "main") -> str:
        """A raw 256-bit SQLCipher key as 64 hex digits."""
        return self._derive(f"database/{name}").hex()

    def session_key(self) -> bytes:
        return self._derive("web/session")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Only this reaches the database, so a stolen copy holds no usable session."""
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()  # a cookie holding bytes that aren't text reaches here as lone surrogates
