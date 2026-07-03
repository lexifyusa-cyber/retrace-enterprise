"""
Security utilities — JWT, password hashing, FastAPI auth dependencies.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import bcrypt as _bcrypt
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_session

bearer_scheme = HTTPBearer(auto_error=False)

ROLES_HIERARCHY = {
    "zero_admin": 110,
    "admin": 100,
    "super_admin": 100,   # legacy alias
    "tenant_admin": 100,  # legacy alias
    "user_admin": 60,
    "user": 20,
}


# ── JWT ────────────────────────────────────────────────────────────────

def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(data: dict[str, Any]) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=7)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_mfa_pending_token(data: dict[str, Any]) -> str:
    """Short-lived token issued after password validation when MFA is required."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=5)
    to_encode.update({"exp": expire, "type": "mfa_pending"})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


# ── Signed download URLs ────────────────────────────────────────────────
# Used so that the Teams page can hand the browser a plain
# `<a href download>` URL, which works inside cross-origin iframes
# (e.g. retrace.me/try). A regular Bearer-header fetch + blob download
# is blocked there because by the time the synthetic <a>.click() fires
# after the file has finished downloading into memory, the iframe's
# transient user-activation flag has expired.
DOWNLOAD_TOKEN_TTL_SECONDS = 60


def create_download_token(user_id: str, pod_id: str) -> str:
    """Issue a short-lived JWT scoped to a single pod download."""
    expire = datetime.now(timezone.utc) + timedelta(seconds=DOWNLOAD_TOKEN_TTL_SECONDS)
    payload = {
        "sub": user_id,
        "pod_id": pod_id,
        "exp": expire,
        "type": "download",
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_download_token(token: str, pod_id: str) -> Optional[str]:
    """Verify a download token and return the user_id, or None if invalid.

    The token is rejected unless the embedded `pod_id` matches *pod_id*,
    so a token leaked for one pod can't be reused to grab another.
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None
    if payload.get("type") != "download":
        return None
    if payload.get("pod_id") != pod_id:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


def verify_token(token: str, expected_type: str = "access") -> dict[str, Any] | None:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("type") != expected_type:
            return None
        return payload
    except JWTError:
        return None


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_secure_token() -> str:
    return secrets.token_urlsafe(32)


# ── Password ───────────────────────────────────────────────────────────

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


DEFAULT_PASSWORD_POLICY = {
    "min_length": 8,
    "require_uppercase": True,
    "require_digit": True,
    "require_special_char": False,
    "max_age_days": 0,
    "history_count": 5,
    "lockout_attempts": 5,
    "lockout_duration_minutes": 15,
}


def validate_password_strength(password: str, policy: dict | None = None) -> Optional[str]:
    """Return an error message if the password is too weak, else None.
    
    When *policy* is supplied it overrides the hardcoded defaults.
    """
    p = {**DEFAULT_PASSWORD_POLICY, **(policy or {})}
    min_len = p.get("min_length", 8)
    if len(password) < min_len:
        return f"Password must be at least {min_len} characters"
    if p.get("require_uppercase", True) and not any(c.isupper() for c in password):
        return "Password must contain at least one uppercase letter"
    if p.get("require_digit", True) and not any(c.isdigit() for c in password):
        return "Password must contain at least one digit"
    if p.get("require_special_char", False) and not any(c in "!@#$%^&*()_+-=[]{}|;':\",./<>?" for c in password):
        return "Password must contain at least one special character"
    return None


# ── FastAPI Dependencies ───────────────────────────────────────────────

class CurrentUser:
    """Decoded JWT claims for the authenticated user."""
    def __init__(self, payload: dict):
        self.user_id: str = payload["sub"]
        self.email: str = payload.get("email", "")
        self.tenant_id: Optional[str] = payload.get("tenant_id")
        self.role: str = payload.get("role", "user")

    @property
    def is_zero_admin(self) -> bool:
        return self.role == "zero_admin"

    @property
    def is_admin(self) -> bool:
        return self.role in ("zero_admin", "admin", "super_admin", "tenant_admin")

    def has_role(self, required_role: str) -> bool:
        return ROLES_HIERARCHY.get(self.role, 0) >= ROLES_HIERARCHY.get(required_role, 0)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> CurrentUser:
    """Extract and validate the JWT from the Authorization header."""
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = verify_token(credentials.credentials, expected_type="access")
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    from app.models.user import User
    user = await session.get(User, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    if user.locked_until and user.locked_until > datetime.utcnow():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is locked")

    return CurrentUser(payload)


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[CurrentUser]:
    """Same as get_current_user but returns None instead of 401 if missing."""
    if not credentials:
        return None
    payload = verify_token(credentials.credentials, expected_type="access")
    if not payload or "sub" not in payload:
        return None
    return CurrentUser(payload)


def require_role(*allowed_roles: str):
    """Dependency factory: raises 403 if the user's role is not high enough."""
    async def _check(current_user: CurrentUser = Depends(get_current_user)):
        for role in allowed_roles:
            if current_user.has_role(role):
                return current_user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    return _check


def require_tenant_match(current_user: CurrentUser, resource_tenant_id: Optional[str]):
    """Raise 403 if the user's tenant doesn't match the resource's tenant."""
    if current_user.is_admin:
        return
    if current_user.tenant_id != resource_tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied — wrong tenant")


def can_provision_role(actor_role: str, target_role: str) -> bool:
    """Role provisioning policy."""
    if target_role == "zero_admin":
        return actor_role == "zero_admin"
    if target_role == "admin":
        return actor_role in ("zero_admin", "admin")
    if target_role == "user_admin":
        return actor_role in ("zero_admin", "admin")
    if target_role == "user":
        return actor_role in ("zero_admin", "admin", "user_admin")
    return False


def can_reset_password(actor_role: str, target_role: str) -> bool:
    """Allow reset when actor outranks target."""
    actor_level = ROLES_HIERARCHY.get(actor_role, 0)
    target_level = ROLES_HIERARCHY.get(target_role, 0)
    return actor_level > target_level


async def get_tenant_team_ids(current_user: CurrentUser, session: AsyncSession) -> list[str]:
    """Get team IDs visible to the current user's tenant.

    zero_admin sees all teams. Other users see only their tenant's teams.
    """
    from app.models.team import Team
    from app.db.expressions import json_bool_true
    from sqlalchemy import or_
    if current_user.role == "zero_admin":
        result = await session.execute(select(Team.team_id))
    else:
        # Include tenant's teams + prebuilt sample teams (tenant_id=None, is_prebuilt=true)
        result = await session.execute(
            select(Team.team_id).where(
                or_(
                    Team.tenant_id == current_user.tenant_id,
                    json_bool_true(Team.metadata_json, "is_prebuilt"),
                )
            )
        )
    return [r[0] for r in result.all()]


# Backwards-compatible alias (to be removed once all callers migrate)
get_tenant_product_ids = get_tenant_team_ids
