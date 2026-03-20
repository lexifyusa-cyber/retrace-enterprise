"""
Security utilities for the gateway layer — key encryption, spending caps, anomaly detection.
"""

import os
import base64
import hashlib
import structlog
from typing import Optional
from cryptography.fernet import Fernet

logger = structlog.get_logger()

_ENCRYPTION_KEY: Optional[bytes] = None


def _get_encryption_key() -> bytes:
    """
    Derive a Fernet key from the JWT_SECRET (always available).
    In production, use a dedicated ENCRYPTION_KEY env var.
    """
    global _ENCRYPTION_KEY
    if _ENCRYPTION_KEY:
        return _ENCRYPTION_KEY

    raw = os.environ.get("ENCRYPTION_KEY") or os.environ.get("JWT_SECRET", "retrace-default-key")
    digest = hashlib.sha256(raw.encode()).digest()
    _ENCRYPTION_KEY = base64.urlsafe_b64encode(digest)
    return _ENCRYPTION_KEY


def encrypt_key(plain_key: str) -> str:
    """Encrypt an API key for storage."""
    f = Fernet(_get_encryption_key())
    return f.encrypt(plain_key.encode()).decode()


def decrypt_key(encrypted_key: str) -> str:
    """Decrypt a stored API key."""
    f = Fernet(_get_encryption_key())
    return f.decrypt(encrypted_key.encode()).decode()


async def check_spending_cap(
    supabase_url: str,
    supabase_service_key: str,
    user_id: str,
    estimated_tokens: int = 0,
) -> tuple[bool, str]:
    """
    Check if a user has exceeded their monthly token budget.
    Queries the Supabase licenses table.

    Returns (allowed: bool, message: str).
    """
    if not supabase_url or not supabase_service_key:
        return True, "No Supabase configured — spending caps not enforced"

    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{supabase_url}/rest/v1/licenses",
                params={"user_id": f"eq.{user_id}", "select": "monthly_token_budget,tokens_used_this_month"},
                headers={
                    "apikey": supabase_service_key,
                    "Authorization": f"Bearer {supabase_service_key}",
                },
            )
            if resp.status_code != 200:
                logger.warning("spending_cap_check_failed", status=resp.status_code)
                return True, "Could not verify spending cap — allowing request"

            rows = resp.json()
            if not rows:
                return True, "No license found — allowing request"

            license_row = rows[0]
            budget = license_row.get("monthly_token_budget", 0)
            used = license_row.get("tokens_used_this_month", 0)

            if budget > 0 and (used + estimated_tokens) > budget:
                return False, f"Monthly usage limit reached ({used:,}/{budget:,} tokens). Upgrade your plan or wait for the next billing cycle."

            return True, "OK"

    except Exception as exc:
        logger.warning("spending_cap_check_error", error=str(exc))
        return True, "Spending cap check unavailable — allowing request"
