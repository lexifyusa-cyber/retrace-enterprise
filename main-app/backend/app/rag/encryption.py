"""
Encryption utilities for chunk text at rest.

Uses AES-256-GCM via the cryptography library. The encryption key is
loaded from the CHUNK_ENCRYPTION_KEY env var (hex-encoded 32-byte key).
If no key is set, a random key is generated once and written to
``backend/.chunk_key`` so it persists across restarts (dev convenience).
"""

import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_ENV = "CHUNK_ENCRYPTION_KEY"
_KEY_FILE = Path(__file__).parent.parent.parent / ".chunk_key"


def _load_key() -> bytes:
    """Return the 32-byte AES key, creating one if needed."""
    hex_key = os.environ.get(_KEY_ENV)
    if hex_key:
        return bytes.fromhex(hex_key)
    if _KEY_FILE.exists():
        return bytes.fromhex(_KEY_FILE.read_text().strip())
    key = secrets.token_bytes(32)
    _KEY_FILE.write_text(key.hex())
    return key


_aesgcm: AESGCM | None = None


def _get_cipher() -> AESGCM:
    global _aesgcm
    if _aesgcm is None:
        _aesgcm = AESGCM(_load_key())
    return _aesgcm


def encrypt_text(plaintext: str) -> bytes:
    """Encrypt UTF-8 text and return nonce (12 bytes) + ciphertext+tag."""
    nonce = os.urandom(12)
    ct = _get_cipher().encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ct


def decrypt_text(data: bytes) -> str:
    """Decrypt bytes produced by encrypt_text back to UTF-8 string."""
    nonce, ct = data[:12], data[12:]
    return _get_cipher().decrypt(nonce, ct, None).decode("utf-8")
