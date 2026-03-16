"""Field-level encryption using Fernet (symmetric key derived from JWT_SECRET)."""

import hashlib
import base64
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _derive_key() -> bytes:
    """Derive a Fernet-compatible key from JWT_SECRET."""
    digest = hashlib.sha256(settings.JWT_SECRET.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_derive_key())


def encrypt_value(plaintext: str) -> str:
    """Encrypt a string value, returning a base64 token."""
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_value(token: str) -> str:
    """Decrypt a Fernet token back to the original string."""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, Exception):
        return token
