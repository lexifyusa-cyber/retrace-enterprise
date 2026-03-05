"""MFA (TOTP) endpoints — setup, verify, disable, and login challenge."""

import io
import base64
from typing import Optional

import pyotp
import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, require_role, CurrentUser, create_access_token, create_refresh_token, verify_token, hash_refresh_token
from app.db.database import get_session
from app.models.user import User
from app.models.user_session import UserSession
from app.models.audit_log import AuditLog
from datetime import datetime, timedelta
from app.core.config import settings

router = APIRouter()


class MFAVerifyRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6)


class MFAChallengeRequest(BaseModel):
    mfa_token: str = Field(...)
    code: str = Field(..., min_length=6, max_length=6)


async def _log_audit(session, action, request, **kwargs):
    log = AuditLog(
        action=action,
        actor_user_id=kwargs.get("actor_id"),
        actor_email=kwargs.get("actor_email"),
        tenant_id=kwargs.get("tenant_id"),
        target_type=kwargs.get("target_type"),
        target_id=kwargs.get("target_id"),
        details=kwargs.get("details"),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", "")[:512],
    )
    session.add(log)


@router.post("/mfa/setup")
async def mfa_setup(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Generate a TOTP secret and QR code for the user to scan."""
    user = await session.get(User, current_user.user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.mfa_enabled:
        raise HTTPException(400, "MFA is already enabled")

    secret = pyotp.random_base32()
    user.mfa_secret_encrypted = secret

    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=user.email, issuer_name="ReTrace")

    img = qrcode.make(provisioning_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return {
        "secret": secret,
        "qr_code": f"data:image/png;base64,{qr_b64}",
        "provisioning_uri": provisioning_uri,
    }


@router.post("/mfa/verify")
async def mfa_verify(
    data: MFAVerifyRequest,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Verify TOTP code and enable MFA for the user."""
    user = await session.get(User, current_user.user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.mfa_enabled:
        raise HTTPException(400, "MFA is already enabled")
    if not user.mfa_secret_encrypted:
        raise HTTPException(400, "Call /mfa/setup first")

    totp = pyotp.TOTP(user.mfa_secret_encrypted)
    if not totp.verify(data.code, valid_window=1):
        raise HTTPException(400, "Invalid verification code")

    user.mfa_enabled = True
    await _log_audit(session, "mfa_enabled", request,
                     actor_id=user.user_id, actor_email=user.email,
                     tenant_id=user.tenant_id, target_type="user", target_id=user.user_id)
    return {"message": "MFA has been enabled successfully"}


@router.post("/mfa/disable")
async def mfa_disable(
    user_id: Optional[str] = None,
    request: Request = None,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Disable MFA. Users can disable their own; admins can disable for others."""
    target_id = user_id or current_user.user_id
    if target_id != current_user.user_id:
        if current_user.role not in ("zero_admin", "admin"):
            raise HTTPException(403, "Only admins can disable MFA for other users")

    user = await session.get(User, target_id)
    if not user:
        raise HTTPException(404, "User not found")
    if not user.mfa_enabled:
        raise HTTPException(400, "MFA is not enabled")

    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    await _log_audit(session, "mfa_disabled", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=user.tenant_id, target_type="user", target_id=target_id)
    return {"message": "MFA has been disabled"}


@router.post("/mfa/challenge")
async def mfa_challenge(
    data: MFAChallengeRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Complete MFA challenge after initial login to get full access tokens."""
    payload = verify_token(data.mfa_token, expected_type="mfa_pending")
    if not payload or "sub" not in payload:
        raise HTTPException(401, "Invalid or expired MFA token")

    user = await session.get(User, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(401, "User not found")
    if not user.mfa_enabled or not user.mfa_secret_encrypted:
        raise HTTPException(400, "MFA is not enabled for this user")

    totp = pyotp.TOTP(user.mfa_secret_encrypted)
    if not totp.verify(data.code, valid_window=1):
        raise HTTPException(400, "Invalid MFA code")

    token_payload = {
        "sub": user.user_id,
        "email": user.email,
        "tenant_id": user.tenant_id,
        "role": user.role,
    }
    access_token = create_access_token(token_payload)
    refresh_token = create_refresh_token(token_payload)

    user_session = UserSession(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        refresh_token_hash=hash_refresh_token(refresh_token),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", "")[:512],
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    session.add(user_session)

    user.last_login_at = datetime.utcnow()
    user.last_login_ip = request.client.host if request.client else None
    user.failed_login_count = 0

    await _log_audit(session, "mfa_challenge_passed", request,
                     actor_id=user.user_id, actor_email=user.email,
                     tenant_id=user.tenant_id)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": user.to_dict(),
    }
