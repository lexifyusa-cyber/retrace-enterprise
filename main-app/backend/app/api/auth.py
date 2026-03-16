"""
Authentication API — register, login (email / LDAP / Azure AD / Google), refresh, logout, user management.
"""

from asyncio import to_thread
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from slowapi import Limiter
from slowapi.util import get_remote_address
import structlog

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    create_mfa_pending_token,
    generate_secure_token,
    hash_password,
    hash_refresh_token,
    verify_password,
    verify_token,
    validate_password_strength,
    get_current_user,
    require_role,
    CurrentUser,
    can_provision_role,
    can_reset_password,
    DEFAULT_PASSWORD_POLICY,
)
from app.db.database import get_session
from app.models.tenant import Tenant
from app.models.user import User
from app.models.user_session import UserSession
from app.models.audit_log import AuditLog
from app.models.password_history import PasswordHistory
from app.services.notifications.smtp_mailer import send_smtp_email

logger = structlog.get_logger()
router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


# ── Schemas ───────────────────────────────────────────────────────────


class TenantRegisterRequest(BaseModel):
    tenant_name: str = Field(..., min_length=2, max_length=255)
    domain: Optional[str] = None
    admin_email: str = Field(...)
    admin_password: str = Field(..., min_length=8)
    admin_display_name: Optional[str] = None
    auth_method: str = Field(default="email")  # email | ldap | azure_ad | google


class LoginRequest(BaseModel):
    email: str = Field(...)
    password: str = Field(...)


class RefreshRequest(BaseModel):
    refresh_token: str


class InviteUserRequest(BaseModel):
    email: str = Field(...)
    display_name: str = Field(default="")
    phone: Optional[str] = None
    role: str = Field(default="user")
    auth_provider: str = Field(default="email")
    department: Optional[str] = None
    timezone: Optional[str] = None
    manager_id: Optional[str] = None
    employee_id: Optional[str] = None
    max_products: Optional[int] = Field(None, ge=0, le=1000)
    max_managed_users: Optional[int] = Field(None, ge=0, le=10000)


class AddUserRequest(BaseModel):
    email: str = Field(...)
    display_name: str = Field(default="")
    phone: Optional[str] = None
    password: Optional[str] = None
    role: str = Field(default="user")
    auth_provider: str = Field(default="email")
    department: Optional[str] = None
    timezone: Optional[str] = None
    manager_id: Optional[str] = None
    employee_id: Optional[str] = None
    max_products: Optional[int] = Field(None, ge=0, le=1000)
    max_managed_users: Optional[int] = Field(None, ge=0, le=10000)


class AddLdapUserRequest(BaseModel):
    username: str = Field(..., min_length=1)
    display_name: str = Field(default="")
    phone: Optional[str] = None
    role: str = Field(default="user")
    department: Optional[str] = None
    timezone: Optional[str] = None
    manager_id: Optional[str] = None
    employee_id: Optional[str] = None
    max_products: Optional[int] = Field(None, ge=0, le=1000)
    max_managed_users: Optional[int] = Field(None, ge=0, le=10000)
    tenant_id: Optional[str] = None


class UpdateUserRoleRequest(BaseModel):
    role: str = Field(...)


class UpdateUserMaxProductsRequest(BaseModel):
    max_products: Optional[int] = Field(None, ge=0, le=1000)


class UpdateUserMaxManagedUsersRequest(BaseModel):
    max_managed_users: Optional[int] = Field(None, ge=0, le=10000)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(default="")
    new_password: str = Field(..., min_length=8)
    new_email: Optional[str] = None


class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = None
    phone: Optional[str] = None
    department: Optional[str] = None
    timezone: Optional[str] = None


class UpdateManagedUserProfileRequest(BaseModel):
    display_name: Optional[str] = None
    phone: Optional[str] = None
    department: Optional[str] = None
    timezone: Optional[str] = None
    manager_id: Optional[str] = None
    employee_id: Optional[str] = None


class AdminSetUserPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8)


class SwitchAuthMethodRequest(BaseModel):
    auth_provider: str = Field(..., pattern="^(email|ldap|azure_ad)$")
    temp_password: Optional[str] = Field(None, min_length=8)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(...)


class ResetPasswordRequest(BaseModel):
    token: str = Field(...)
    new_password: str = Field(..., min_length=8)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: dict


# ── Helpers ───────────────────────────────────────────────────────────


async def _log_audit(
    session: AsyncSession,
    action: str,
    request: Request,
    actor_id: Optional[str] = None,
    actor_email: Optional[str] = None,
    tenant_id: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[dict] = None,
):
    log = AuditLog(
        action=action,
        actor_user_id=actor_id,
        actor_email=actor_email,
        tenant_id=tenant_id,
        target_type=target_type,
        target_id=target_id,
        details=details,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", "")[:512],
    )
    session.add(log)


def _require_valid_email_for_non_zero_admin(email: str, role: str) -> None:
    """Raise 400 if role is not zero_admin and email is missing or invalid (no @)."""
    if role == "zero_admin":
        return
    if not email or not isinstance(email, str) or "@" not in email.strip():
        raise HTTPException(400, "A valid email address is required for this role")


def _ensure_user_admin_lob_access(current_user: CurrentUser, target_user: User):
    """User Admins can only access users explicitly assigned to their own LOB."""
    if current_user.role != "user_admin":
        return
    if target_user.role != "user" or target_user.manager_id != current_user.user_id:
        raise HTTPException(404, "User not found")


async def _assert_single_zero_admin(session: AsyncSession, exclude_user_id: Optional[str] = None):
    """Raise 400 if another zero_admin already exists (only one per deployment)."""
    q = select(func.count(User.user_id)).where(User.role == "zero_admin")
    if exclude_user_id:
        q = q.where(User.user_id != exclude_user_id)
    count = (await session.execute(q)).scalar() or 0
    if count > 0:
        raise HTTPException(400, "Only one Zero Admin is allowed per deployment")


async def _validate_user_lob_manager(
    session: AsyncSession,
    current_user: CurrentUser,
    tenant_id: Optional[str],
    target_role: str,
    manager_id: Optional[str],
):
    """Ensure users are explicitly associated to a User Admin LOB where required."""
    if target_role != "user":
        return
    if current_user.role == "user_admin":
        return
    if not manager_id:
        raise HTTPException(400, "manager_id (User Admin) is required for users")
    manager = await session.get(User, manager_id)
    if not manager or manager.role != "user_admin":
        raise HTTPException(400, "manager_id must reference a valid User Admin")
    if tenant_id is not None and manager.tenant_id != tenant_id:
        raise HTTPException(400, "manager_id must belong to the same organization")


def _mask_smtp_config(smtp_config: Optional[dict]) -> Optional[dict]:
    if not smtp_config:
        return None
    cfg = dict(smtp_config)
    if cfg.get("password"):
        cfg["password"] = "********"
    return cfg


async def _resolve_tenant_id(
    session: AsyncSession, current_user: "CurrentUser", data: Optional[dict] = None
) -> str:
    """Resolve the target tenant_id for config endpoints.
    Falls back to the single tenant row for single-tenant deployments."""
    tid = current_user.tenant_id or (data.get("tenant_id") if data else None)
    if not tid:
        tid = await _get_single_tenant_id(session)
    if not tid:
        raise HTTPException(400, "No tenant configured. Please contact your administrator.")
    return tid


async def _get_single_tenant_id(session: AsyncSession) -> Optional[str]:
    """Return the first (and typically only) tenant id for single-tenant deployments.
    Used when current_user.tenant_id or target.tenant_id is None (e.g. zero_admin)."""
    result = await session.execute(select(Tenant.tenant_id).limit(1))
    row = result.scalar_one_or_none()
    return row


async def _get_password_policy(session: AsyncSession, tenant_id: Optional[str]) -> dict:
    """Return the merged password policy for the tenant (defaults + overrides)."""
    policy = dict(DEFAULT_PASSWORD_POLICY)
    if tenant_id:
        tenant = await session.get(Tenant, tenant_id)
        if tenant and tenant.password_policy:
            policy.update(tenant.password_policy)
    return policy


async def _check_password_history(
    session: AsyncSession, user_id: str, new_password: str, history_count: int
) -> Optional[str]:
    """Return error message if the new password was recently used, else None."""
    if history_count <= 0:
        return None
    result = await session.execute(
        select(PasswordHistory)
        .where(PasswordHistory.user_id == user_id)
        .order_by(PasswordHistory.created_at.desc())
        .limit(history_count)
    )
    for entry in result.scalars().all():
        if verify_password(new_password, entry.hashed_password):
            return f"Cannot reuse any of your last {history_count} passwords"
    return None


async def _record_password_history(session: AsyncSession, user_id: str, hashed_password: str):
    """Save the hashed password to history."""
    session.add(PasswordHistory(user_id=user_id, hashed_password=hashed_password))


async def _send_invite_email(
    session: AsyncSession,
    tenant_id: Optional[str],
    *,
    to_email: str,
    display_name: str,
    invite_token: str,
) -> tuple[bool, Optional[str]]:
    """Send an invite email with a setup link (no password exposed)."""
    tid = tenant_id or await _get_single_tenant_id(session)
    if not tid:
        return False, "No tenant context for SMTP email"
    tenant = await session.get(Tenant, tid)
    if not tenant or not tenant.smtp_config:
        return False, "SMTP is not configured"
    cfg = dict(tenant.smtp_config)
    if not cfg.get("enabled", True):
        return False, "SMTP is disabled"
    first_name = (display_name or to_email).split(" ")[0]
    setup_link = f"http://localhost:5173/setup?token={invite_token}"
    subject = "You're invited to ReTrace"
    html = f"""
    <html><body style="font-family: Arial, sans-serif; color: #111;">
      <p>Hello {first_name},</p>
      <p>You've been invited to join ReTrace. Click the link below to set up your account and create your password:</p>
      <p><a href="{setup_link}" style="background: #4F46E5; color: #fff; padding: 10px 20px; text-decoration: none; border-radius: 6px;">Set Up Your Account</a></p>
      <p>This link is valid for 7 days.</p>
    </body></html>
    """
    text = (
        f"Hello {first_name},\n\n"
        f"You've been invited to join ReTrace.\n"
        f"Set up your account here (valid for 7 days): {setup_link}\n"
    )
    try:
        await to_thread(send_smtp_email, cfg, to_email=to_email, subject=subject, html_body=html, text_body=text)
        return True, None
    except Exception as exc:
        return False, str(exc)


async def _send_temp_password_email(
    session: AsyncSession,
    tenant_id: Optional[str],
    *,
    to_email: str,
    display_name: str,
    temp_password: str,
    purpose: str,
) -> tuple[bool, Optional[str]]:
    tid = tenant_id or await _get_single_tenant_id(session)
    if not tid:
        return False, "No tenant context for SMTP email"
    tenant = await session.get(Tenant, tid)
    if not tenant or not tenant.smtp_config:
        return False, "SMTP is not configured"
    cfg = dict(tenant.smtp_config)
    if not cfg.get("enabled", True):
        return False, "SMTP is disabled"
    first_name = (display_name or to_email).split(" ")[0]
    subject = f"Your ReTrace {purpose}"
    html = f"""
    <html><body style="font-family: Arial, sans-serif; color: #111;">
      <p>Hello {first_name},</p>
      <p>Your ReTrace account {purpose} has been processed.</p>
      <p><b>Temporary password:</b> {temp_password}</p>
      <p>Please sign in and change your password immediately.</p>
    </body></html>
    """
    text = (
        f"Hello {first_name},\n\n"
        f"Your ReTrace account {purpose} has been processed.\n"
        f"Temporary password: {temp_password}\n\n"
        "Please sign in and change your password immediately."
    )
    try:
        await to_thread(
            send_smtp_email,
            cfg,
            to_email=to_email,
            subject=subject,
            html_body=html,
            text_body=text,
        )
        return True, None
    except Exception as exc:
        return False, str(exc)


def _build_token_payload(user: User) -> dict:
    return {
        "sub": user.user_id,
        "email": user.email,
        "tenant_id": user.tenant_id,
        "role": user.role,
    }


async def _create_session_and_tokens(
    user: User,
    request: Request,
    session: AsyncSession,
) -> dict:
    if user.mfa_enabled and user.mfa_secret_encrypted and not user.force_password_change:
        mfa_token = create_mfa_pending_token(_build_token_payload(user))
        return {
            "mfa_required": True,
            "mfa_token": mfa_token,
            "user": user.to_dict(),
        }

    payload = _build_token_payload(user)
    access_token = create_access_token(payload)
    refresh_token = create_refresh_token(payload)

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

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=user.to_dict(),
    )


# ── Register Tenant ──────────────────────────────────────────────────


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register_tenant(
    data: TenantRegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Register a new tenant (organization) with an admin user."""
    # Check for existing email
    existing = await session.execute(select(User).where(User.email == data.admin_email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Email already registered")

    # Check for existing domain
    if data.domain:
        existing_tenant = await session.execute(select(Tenant).where(Tenant.domain == data.domain.lower()))
        if existing_tenant.scalar_one_or_none():
            raise HTTPException(400, "Domain already registered")

    pw_error = validate_password_strength(data.admin_password)
    if pw_error:
        raise HTTPException(400, pw_error)

    tenant = Tenant(
        name=data.tenant_name,
        domain=data.domain.lower() if data.domain else None,
        auth_method=data.auth_method,
    )
    session.add(tenant)
    await session.flush()

    user = User(
        tenant_id=tenant.tenant_id,
        email=data.admin_email.lower(),
        display_name=data.admin_display_name or data.admin_email.split("@")[0],
        hashed_password=hash_password(data.admin_password),
        role="admin",
        auth_provider="email",
    )
    session.add(user)
    await session.flush()

    await _log_audit(session, "tenant_registered", request,
                     actor_id=user.user_id, actor_email=user.email,
                     tenant_id=tenant.tenant_id, target_type="tenant", target_id=tenant.tenant_id,
                     details={"tenant_name": tenant.name})

    result = await _create_session_and_tokens(user, request, session)
    logger.info("tenant_registered", tenant_id=tenant.tenant_id, email=user.email)
    return result


# ── Login (unified — auto-detects local vs LDAP) ─────────────────────


class UnifiedLoginRequest(BaseModel):
    identifier: str = Field(...)
    password: str = Field(...)


async def _get_single_tenant(session: AsyncSession) -> Optional["Tenant"]:
    result = await session.execute(select(Tenant).where(Tenant.status == "active").limit(2))
    tenants = result.scalars().all()
    return tenants[0] if len(tenants) == 1 else None


def _email_suffixes(tenant: "Tenant") -> list[str]:
    suffixes = []
    if tenant.domain:
        suffixes.append(tenant.domain.strip().lower())
    if tenant.ldap_config:
        host = tenant.ldap_config.get("host", "")
        if host:
            cleaned = host.replace("ldap://", "").replace("ldaps://", "").strip("/")
            if cleaned and cleaned not in suffixes:
                suffixes.append(cleaned)
    return suffixes


async def _resolve_user_for_login(
    session: AsyncSession, identifier: str
) -> tuple[Optional["User"], Optional["Tenant"], str]:
    ident = identifier.strip().lower()
    result = await session.execute(select(User).where(User.email == ident))
    user = result.scalar_one_or_none()
    if user:
        tenant = await session.get(Tenant, user.tenant_id) if user.tenant_id else None
        return user, tenant, "resolved_by_email"

    result = await session.execute(select(User).where(User.email == ident, User.tenant_id == None))
    user = result.scalar_one_or_none()
    if user:
        return user, None, "resolved_global_admin"

    tenant = await _get_single_tenant(session)
    if not tenant:
        return None, None, "no_tenant_configured"

    result = await session.execute(select(User).where(User.tenant_id == tenant.tenant_id, User.username == ident))
    user = result.scalar_one_or_none()
    if user:
        return user, tenant, "resolved_by_username"

    for suffix in _email_suffixes(tenant):
        candidate = f"{ident}@{suffix}"
        result = await session.execute(select(User).where(User.tenant_id == tenant.tenant_id, User.email == candidate))
        user = result.scalar_one_or_none()
        if user:
            return user, tenant, "resolved_by_constructed_email"

    return None, tenant, "user_not_found_in_tenant"


# ── Auth Options (public — login page discovery) ─────────────────────


@router.get("/options")
@limiter.limit("10/minute")
async def auth_options(
    identifier: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Return which auth method a given identifier should use.
    Public endpoint — does not reveal whether a user account exists."""
    ident = identifier.strip().lower()
    user, tenant, _ = await _resolve_user_for_login(session, ident)

    domain: Optional[str] = None
    if "@" in ident:
        domain = ident.split("@", 1)[1]

    if user:
        provider = user.auth_provider or "email"
        t = tenant or (await session.get(Tenant, user.tenant_id) if user.tenant_id else None)
        return {"auth_method": provider, "domain": t.domain if t else domain, "identifier": ident}

    if tenant:
        if tenant.azure_ad_config and domain:
            return {"auth_method": "azure_ad", "domain": tenant.domain or domain, "identifier": ident}
        if tenant.ldap_config:
            return {"auth_method": "ldap", "domain": tenant.domain or domain, "identifier": ident}
        return {"auth_method": "email", "domain": tenant.domain or domain, "identifier": ident}

    return {"auth_method": "email", "domain": domain, "identifier": ident}


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login(
    data: UnifiedLoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Unified login — single-org model. Auto-detects local vs LDAP."""
    ident = data.identifier.strip().lower()
    user, tenant, resolve_reason = await _resolve_user_for_login(session, ident)

    if not user and tenant and tenant.ldap_config:
        from app.services.auth.ldap_auth import authenticate_ldap
        ldap_result = await authenticate_ldap(ident, data.password, tenant.ldap_config)
        if ldap_result:
            email = (ldap_result.email or f"{ident}@ldap").strip().lower() or f"{ident}@ldap"
            existing = await session.execute(select(User).where(User.email == email))
            existing_user = existing.scalar_one_or_none()
            if existing_user and existing_user.auth_provider == "email":
                logger.info("unified_login_ldap_jit_blocked_local_user", email=email)
            elif existing_user:
                user = existing_user
                resolve_reason = "resolved_by_ldap_jit_existing"
            else:
                user = User(
                    tenant_id=tenant.tenant_id,
                    email=email,
                    username=ident,
                    display_name=ldap_result.display_name,
                    auth_provider="ldap",
                    role="user",
                    status="active",
                )
                session.add(user)
                await session.flush()
                resolve_reason = "ldap_jit_provisioned"

    if not user:
        await _log_audit(session, "login_failed", request, actor_email=ident,
                         tenant_id=tenant.tenant_id if tenant else None,
                         details={"reason_code": resolve_reason, "identifier": ident[:50]})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")

    if not user.is_active:
        await _log_audit(session, "login_failed", request, actor_id=user.user_id, actor_email=user.email,
                         tenant_id=user.tenant_id, details={"reason_code": "account_inactive"})
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is deactivated")

    if user.locked_until and user.locked_until > datetime.utcnow():
        remaining = int((user.locked_until - datetime.utcnow()).total_seconds() / 60) + 1
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Account locked. Try again in {remaining} minutes.")

    if user.tenant_id and tenant and tenant.status == "suspended":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization is suspended")

    # --- Auth dispatch based on auth_provider ---
    auth_provider = user.auth_provider or "email"

    if auth_provider == "ldap":
        # Must authenticate against LDAP
        if not tenant or not tenant.ldap_config:
            await _log_audit(session, "login_failed", request, actor_id=user.user_id,
                             actor_email=user.email, tenant_id=user.tenant_id,
                             details={"reason_code": "ldap_config_missing"})
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "Your organization's sign-in service is not configured. Contact your administrator.")

        if getattr(tenant, "ldap_require_ssl", False) and not tenant.ldap_config.get("use_ssl"):
            await _log_audit(session, "login_failed", request, actor_id=user.user_id,
                             actor_email=user.email, tenant_id=user.tenant_id,
                             details={"reason_code": "ldap_tls_required_but_disabled"})
            raise HTTPException(400, "Your organization's sign-in service requires a secure connection. Contact your administrator.")

        if resolve_reason in ("ldap_jit_provisioned", "resolved_by_ldap_jit_existing"):
            # Already authenticated via LDAP JIT above
            pass
        else:
            from app.services.auth.ldap_auth import authenticate_ldap
            ldap_result = await authenticate_ldap(
                user.username or ident, data.password, tenant.ldap_config
            )
            if not ldap_result:
                await _log_audit(session, "login_failed", request, actor_id=user.user_id,
                                 actor_email=user.email, tenant_id=user.tenant_id,
                                 details={"reason_code": "ldap_auth_failed", "ldap_username": (user.username or ident)[:50]})
                raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                    "LDAP sign-in failed. Check your username and password.")

        await _log_audit(session, "login", request, actor_id=user.user_id, actor_email=user.email,
                         tenant_id=user.tenant_id,
                         details={"provider": "ldap", "resolve": resolve_reason})
        logger.info("unified_login_ldap_success", user_id=user.user_id, email=user.email)
        return await _create_session_and_tokens(user, request, session)

    # --- Local (email/password) auth ---
    if not user.hashed_password or not verify_password(data.password, user.hashed_password):
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= settings.MAX_FAILED_LOGINS:
            user.locked_until = datetime.utcnow() + timedelta(minutes=settings.LOCKOUT_MINUTES)
            await _log_audit(session, "account_locked", request, actor_id=user.user_id,
                             actor_email=user.email, tenant_id=user.tenant_id,
                             details={"failed_attempts": user.failed_login_count})
        await _log_audit(session, "login_failed", request, actor_id=user.user_id,
                         actor_email=user.email, tenant_id=user.tenant_id,
                         details={"reason_code": "local_bad_password", "attempt": user.failed_login_count})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    # Check password expiration (max_age_days)
    if user.auth_provider == "email" and tenant:
        policy = await _get_password_policy(session, user.tenant_id)
        max_age = policy.get("max_age_days", 0)
        if max_age and max_age > 0:
            pw_date = getattr(user, "password_changed_at", None) or user.created_at
            if pw_date and (datetime.utcnow() - pw_date).days >= max_age:
                user.force_password_change = True

    await _log_audit(session, "login", request, actor_id=user.user_id, actor_email=user.email,
                     tenant_id=user.tenant_id,
                     details={"provider": "email", "resolve": resolve_reason})
    logger.info("unified_login_local_success", user_id=user.user_id, email=user.email)
    return await _create_session_and_tokens(user, request, session)


# ── Refresh Token ─────────────────────────────────────────────────────


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    data: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    payload = verify_token(data.refresh_token, expected_type="refresh")
    if not payload or "sub" not in payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")

    token_hash = hash_refresh_token(data.refresh_token)
    result = await session.execute(
        select(UserSession).where(
            UserSession.refresh_token_hash == token_hash,
            UserSession.revoked == False,
        )
    )
    user_session = result.scalar_one_or_none()
    if not user_session or user_session.expires_at < datetime.utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired or revoked")

    user = await session.get(User, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")

    # Revoke old session, create new one (rotation)
    user_session.revoked = True

    return await _create_session_and_tokens(user, request, session)


# ── Logout ────────────────────────────────────────────────────────────


@router.post("/logout")
async def logout(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    # Revoke all active sessions for this user
    result = await session.execute(
        select(UserSession).where(
            UserSession.user_id == current_user.user_id,
            UserSession.revoked == False,
        )
    )
    for s in result.scalars().all():
        s.revoked = True

    await _log_audit(session, "logout", request, actor_id=current_user.user_id,
                     actor_email=current_user.email, tenant_id=current_user.tenant_id)
    return {"message": "Logged out"}


# ── Me ────────────────────────────────────────────────────────────────


@router.get("/me")
async def get_me(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, current_user.user_id)
    if not user:
        raise HTTPException(404, "User not found")
    tenant = None
    if user.tenant_id:
        t = await session.get(Tenant, user.tenant_id)
        tenant = t.to_dict() if t else None
    return {"user": user.to_dict(), "tenant": tenant}


@router.patch("/me/profile")
async def update_my_profile(
    data: UpdateProfileRequest,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, current_user.user_id)
    if not user:
        raise HTTPException(404, "User not found")

    if data.display_name is not None:
        user.display_name = data.display_name.strip()
    if data.phone is not None:
        user.phone = data.phone.strip() or None
    if data.department is not None:
        user.department = data.department.strip() or None
    if data.timezone is not None:
        user.timezone = data.timezone.strip() or None

    await _log_audit(session, "profile_updated", request,
                     actor_id=user.user_id, actor_email=user.email,
                     tenant_id=user.tenant_id, target_type="user", target_id=user.user_id)
    return {"user": user.to_dict()}


# ── Change Password / First Login Setup ──────────────────────────────


@router.post("/change-password")
async def change_password(
    data: ChangePasswordRequest,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Change password. On first login (force_password_change), current_password is optional.
    Also allows changing the admin email/username on first login."""
    user = await session.get(User, current_user.user_id)
    if not user:
        raise HTTPException(404, "User not found")

    if not user.force_password_change:
        if not data.current_password:
            raise HTTPException(400, "Current password is required")
        if not user.hashed_password or not verify_password(data.current_password, user.hashed_password):
            raise HTTPException(400, "Current password is incorrect")

    policy = await _get_password_policy(session, user.tenant_id)
    pw_error = validate_password_strength(data.new_password, policy)
    if pw_error:
        raise HTTPException(400, pw_error)

    history_error = await _check_password_history(
        session, user.user_id, data.new_password, policy.get("history_count", 5)
    )
    if history_error:
        raise HTTPException(400, history_error)

    new_hash = hash_password(data.new_password)
    await _record_password_history(session, user.user_id, new_hash)
    user.hashed_password = new_hash
    user.force_password_change = False
    user.password_changed_at = datetime.utcnow()
    if user.status == "invited":
        user.status = "active"

    if data.new_email and data.new_email.strip():
        new_email = data.new_email.strip().lower()
        if new_email != user.email:
            existing = await session.execute(select(User).where(User.email == new_email))
            if existing.scalar_one_or_none():
                raise HTTPException(400, "Email already in use")
            user.email = new_email

    await _log_audit(session, "password_changed", request,
                     actor_id=user.user_id, actor_email=user.email,
                     tenant_id=user.tenant_id, target_type="user", target_id=user.user_id)

    result = await _create_session_and_tokens(user, request, session)
    return result


# ── Forgot / Reset Password (self-service) ───────────────────────────


@router.post("/forgot-password")
@limiter.limit("5/minute")
async def forgot_password(
    data: ForgotPasswordRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Send a password-reset link to the user's email (if the account exists and uses local auth)."""
    email = data.email.strip().lower()
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user and user.is_active and user.auth_provider == "email":
        token = generate_secure_token()
        user.password_reset_token = token
        user.password_reset_expires = datetime.utcnow() + timedelta(hours=1)

        if user.tenant_id:
            tenant = await session.get(Tenant, user.tenant_id)
            smtp_cfg = tenant.smtp_config if tenant else None
            if smtp_cfg and smtp_cfg.get("enabled", True):
                first_name = (user.display_name or email).split(" ")[0]
                reset_link = f"http://localhost:5173/reset-password?token={token}"
                html = f"""
                <html><body style="font-family: Arial, sans-serif; color: #111;">
                  <p>Hello {first_name},</p>
                  <p>We received a request to reset your password. Click the link below within 1 hour:</p>
                  <p><a href="{reset_link}" style="background: #4F46E5; color: #fff; padding: 10px 20px; text-decoration: none; border-radius: 6px;">Reset Password</a></p>
                  <p>If you didn't request this, you can safely ignore this email.</p>
                </body></html>
                """
                text = (
                    f"Hello {first_name},\n\n"
                    f"Reset your password using this link (valid for 1 hour):\n{reset_link}\n\n"
                    "If you didn't request this, you can safely ignore this email."
                )
                try:
                    await to_thread(
                        send_smtp_email, smtp_cfg,
                        to_email=email, subject="ReTrace — Password Reset",
                        html_body=html, text_body=text,
                    )
                except Exception:
                    pass

        await _log_audit(session, "password_reset_requested", request,
                         actor_email=email, tenant_id=user.tenant_id if user else None)

    return {"message": "If an account exists with that email, we've sent a reset link."}


@router.post("/reset-password")
@limiter.limit("5/minute")
async def reset_password(
    data: ResetPasswordRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Reset password using a token from the forgot-password email."""
    result = await session.execute(
        select(User).where(
            User.password_reset_token == data.token,
            User.password_reset_expires > datetime.utcnow(),
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(400, "Invalid or expired reset token")
    if not user.is_active:
        raise HTTPException(403, "Account is deactivated")

    policy = await _get_password_policy(session, user.tenant_id)
    pw_error = validate_password_strength(data.new_password, policy)
    if pw_error:
        raise HTTPException(400, pw_error)

    history_error = await _check_password_history(
        session, user.user_id, data.new_password, policy.get("history_count", 5)
    )
    if history_error:
        raise HTTPException(400, history_error)

    new_hash = hash_password(data.new_password)
    await _record_password_history(session, user.user_id, new_hash)
    user.hashed_password = new_hash
    user.force_password_change = False
    user.password_changed_at = datetime.utcnow()
    user.password_reset_token = None
    user.password_reset_expires = None

    await _log_audit(session, "password_reset_completed", request,
                     actor_id=user.user_id, actor_email=user.email,
                     tenant_id=user.tenant_id, target_type="user", target_id=user.user_id)
    return {"message": "Password has been reset successfully. You can now sign in."}


@router.get("/invite/validate")
async def validate_invite_token(
    token: str,
    session: AsyncSession = Depends(get_session),
):
    """Validate an invite token and return auth_provider without consuming the token. Public."""
    if not token or not token.strip():
        return {"valid": False}
    result = await session.execute(
        select(User).where(
            User.password_reset_token == token.strip(),
            User.password_reset_expires > datetime.utcnow(),
            User.status == "invited",
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        return {"valid": False}
    return {"valid": True, "auth_provider": user.auth_provider or "email"}


@router.post("/accept-invite")
async def accept_invite(
    data: ResetPasswordRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Accept an invite: validate the token and let the user set their own password."""
    result = await session.execute(
        select(User).where(
            User.password_reset_token == data.token,
            User.password_reset_expires > datetime.utcnow(),
            User.status == "invited",
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(400, "Invalid or expired invite token")
    if user.auth_provider == "azure_ad":
        raise HTTPException(400, "Azure AD users should sign in with Microsoft to complete the invitation.")

    policy = await _get_password_policy(session, user.tenant_id)
    pw_error = validate_password_strength(data.new_password, policy)
    if pw_error:
        raise HTTPException(400, pw_error)

    new_hash = hash_password(data.new_password)
    await _record_password_history(session, user.user_id, new_hash)
    user.hashed_password = new_hash
    user.force_password_change = False
    user.password_changed_at = datetime.utcnow()
    user.password_reset_token = None
    user.password_reset_expires = None
    user.status = "active"

    await _log_audit(session, "invite_accepted", request,
                     actor_id=user.user_id, actor_email=user.email,
                     tenant_id=user.tenant_id, target_type="user", target_id=user.user_id)
    return await _create_session_and_tokens(user, request, session)


@router.post("/users/resend-invite")
async def resend_invite(
    data: dict,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    """Resend an invite email to a user who hasn't accepted yet."""
    user_id = data.get("user_id")
    if not user_id:
        raise HTTPException(400, "user_id is required")
    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target.status != "invited":
        raise HTTPException(400, "User has already accepted the invite")
    if current_user.tenant_id and target.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")
    if current_user.role == "user_admin":
        _ensure_user_admin_lob_access(current_user, target)

    invite_token = generate_secure_token()
    target.password_reset_token = invite_token
    target.password_reset_expires = datetime.utcnow() + timedelta(days=7)

    email_sent, email_error = await _send_invite_email(
        session, target.tenant_id,
        to_email=target.email,
        display_name=target.display_name or target.email,
        invite_token=invite_token,
    )
    await _log_audit(session, "invite_resent", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id, target_type="user", target_id=user_id)
    return {"message": "Invite resent", "email_sent": email_sent, "email_error": email_error}


@router.patch("/users/{user_id}/profile")
async def update_managed_user_profile(
    user_id: str,
    data: UpdateManagedUserProfileRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    """Admin/User Admin can update profiles of users they are allowed to manage."""
    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if current_user.tenant_id is not None and target.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")

    if current_user.role == "user_admin":
        _ensure_user_admin_lob_access(current_user, target)

    if current_user.role in ("admin", "super_admin", "tenant_admin") and target.role not in ("admin", "user_admin", "user"):
        raise HTTPException(403, "You cannot edit this profile")

    if data.display_name is not None:
        target.display_name = data.display_name.strip()
    if data.phone is not None:
        target.phone = data.phone.strip() or None
    if data.department is not None:
        target.department = data.department.strip() or None
    if data.timezone is not None:
        target.timezone = data.timezone.strip() or None
    if data.employee_id is not None:
        target.employee_id = data.employee_id.strip() or None
    if data.manager_id is not None:
        if current_user.role == "user_admin":
            raise HTTPException(403, "User Admin cannot reassign LOB manager")
        manager_id = data.manager_id.strip() or None
        if manager_id:
            manager = await session.get(User, manager_id)
            if not manager or manager.role != "user_admin":
                raise HTTPException(400, "manager_id must reference a valid User Admin")
            if target.tenant_id and manager.tenant_id != target.tenant_id:
                raise HTTPException(400, "manager_id must belong to the same organization")
        target.manager_id = manager_id

    await _log_audit(session, "managed_profile_updated", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id, target_type="user", target_id=user_id)
    return {"user": target.to_dict()}


@router.post("/users/{user_id}/reset-password")
async def admin_reset_user_password(
    user_id: str,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    """Admin-initiated password reset. System generates a random temporary password,
    emails it to the user, and forces a password change on next login.
    The admin never sees the password.
    """
    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if current_user.tenant_id is not None and target.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")

    if current_user.role == "user_admin":
        if target.role != "user" or target.manager_id != current_user.user_id:
            raise HTTPException(404, "User not found")
        if target.auth_provider != "email":
            raise HTTPException(400, "LDAP user passwords must be changed in LDAP")
    else:
        if target.role not in ("admin", "user_admin", "user"):
            raise HTTPException(403, "You cannot reset this user's password")
        if target.auth_provider == "ldap":
            raise HTTPException(400, "LDAP user passwords must be changed in LDAP")

    if target.role != "zero_admin":
        if not target.email or "@" not in (target.email or "").strip():
            raise HTTPException(400, "User must have a valid email address to receive the temporary password")

    temp_password = generate_secure_token()[:12] + "A1!"
    new_hash = hash_password(temp_password)
    await _record_password_history(session, target.user_id, new_hash)
    target.hashed_password = new_hash
    target.force_password_change = True
    target.password_changed_at = datetime.utcnow()

    email_sent = False
    email_error: Optional[str] = None
    if target.role != "zero_admin" and target.email and "@" in target.email:
        email_sent, email_error = await _send_temp_password_email(
            session,
            target.tenant_id,
            to_email=target.email,
            display_name=target.display_name or target.email,
            temp_password=temp_password,
            purpose="password reset",
        )

    await _log_audit(session, "managed_password_reset", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id, target_type="user", target_id=user_id)
    return {
        "message": "Temporary password has been generated and emailed to the user",
        "email_sent": email_sent,
        "email_error": email_error,
    }


@router.patch("/users/{user_id}/auth-method")
async def switch_auth_method(
    user_id: str,
    data: SwitchAuthMethodRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    """Switch a user between email and LDAP authentication."""
    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if current_user.tenant_id is not None and target.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")

    # Permission scoping
    if current_user.role == "user_admin":
        if target.role != "user" or target.manager_id != current_user.user_id:
            raise HTTPException(404, "User not found")
    elif current_user.role in ("admin",):
        if target.role not in ("admin", "user_admin", "user"):
            raise HTTPException(403, "Cannot change auth method for this user")
    # zero_admin can change anyone except switching zero_admin to LDAP

    if target.role == "zero_admin" and data.auth_provider in ("ldap", "azure_ad"):
        raise HTTPException(400, "Zero Admin must use email authentication")

    if target.auth_provider == data.auth_provider:
        raise HTTPException(400, f"User is already using {data.auth_provider} authentication")

    old_provider = target.auth_provider
    if data.auth_provider == "ldap":
        target.auth_provider = "ldap"
        target.hashed_password = None
    elif data.auth_provider == "azure_ad":
        target.auth_provider = "azure_ad"
        target.hashed_password = None
    elif data.auth_provider == "email":
        if not data.temp_password:
            raise HTTPException(400, "temp_password is required when switching to email")
        pw_error = validate_password_strength(data.temp_password)
        if pw_error:
            raise HTTPException(400, pw_error)
        target.auth_provider = "email"
        target.hashed_password = hash_password(data.temp_password)
        target.force_password_change = True

    await _log_audit(session, "auth_method_changed", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id,
                     target_type="user", target_id=user_id,
                     details={"old_provider": old_provider, "new_provider": data.auth_provider})
    return {"message": f"Authentication method changed to {data.auth_provider}", "user": target.to_dict()}


# ── User Management (Tenant Admin / User Admin) ──────────────────────


@router.get("/users")
async def list_users(
    search: Optional[str] = None,
    role: Optional[str] = None,
    status_filter: Optional[str] = None,
    auth_provider: Optional[str] = None,
    manager_id: Optional[str] = None,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    page: int = 1,
    per_page: int = 50,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    """List users with LOB-aware scoping, search, filters, and pagination."""
    query = select(User)
    count_query = select(func.count(User.user_id))

    if current_user.tenant_id:
        query = query.where(User.tenant_id == current_user.tenant_id)
        count_query = count_query.where(User.tenant_id == current_user.tenant_id)
    if current_user.role == "user_admin":
        lob_filter = (User.role == "user") & (User.manager_id == current_user.user_id)
        query = query.where(lob_filter)
        count_query = count_query.where(lob_filter)

    if search:
        s = f"%{search.strip().lower()}%"
        search_filter = (
            User.email.ilike(s) | User.display_name.ilike(s) |
            User.username.ilike(s) | User.department.ilike(s)
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)
    if role:
        query = query.where(User.role == role)
        count_query = count_query.where(User.role == role)
    if status_filter:
        query = query.where(User.status == status_filter)
        count_query = count_query.where(User.status == status_filter)
    if auth_provider:
        query = query.where(User.auth_provider == auth_provider)
        count_query = count_query.where(User.auth_provider == auth_provider)
    if manager_id:
        query = query.where(User.manager_id == manager_id)
        count_query = count_query.where(User.manager_id == manager_id)

    sort_col = getattr(User, sort_by, User.created_at)
    query = query.order_by(sort_col.desc() if sort_dir == "desc" else sort_col.asc())

    total = (await session.execute(count_query)).scalar() or 0
    offset = (max(1, page) - 1) * per_page
    query = query.offset(offset).limit(per_page)

    result = await session.execute(query)
    return {
        "users": [u.to_dict() for u in result.scalars().all()],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }


@router.post("/users/invite", status_code=201)
async def invite_user(
    data: InviteUserRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    if not can_provision_role(current_user.role, data.role):
        raise HTTPException(403, "You are not allowed to assign this role")
    if data.role == "zero_admin":
        await _assert_single_zero_admin(session)
    _require_valid_email_for_non_zero_admin((data.email or "").strip(), data.role)

    resolved_tenant_id = current_user.tenant_id or await _get_single_tenant_id(session)
    if not resolved_tenant_id:
        raise HTTPException(400, "No tenant configured. Configure organization in Settings first.")

    existing = await session.execute(select(User).where(User.email == data.email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Email already registered")

    auth_provider = (data.auth_provider or "email").lower()
    if auth_provider not in ("email", "ldap", "azure_ad"):
        raise HTTPException(400, "auth_provider must be email, ldap, or azure_ad")
    if auth_provider in ("ldap", "azure_ad") and data.role == "zero_admin":
        raise HTTPException(400, "Zero Admin cannot use LDAP or Azure AD")
    if data.role == "zero_admin":
        auth_provider = "email"
    await _validate_user_lob_manager(
        session,
        current_user,
        resolved_tenant_id,
        data.role,
        data.manager_id,
    )

    # Enforce max_managed_users when user_admin creates a user
    if current_user.role == "user_admin" and data.role == "user":
        creator = await session.get(User, current_user.user_id)
        tenant = await session.get(Tenant, resolved_tenant_id)
        effective_limit = (
            creator.max_managed_users
            if creator and creator.max_managed_users is not None
            else (getattr(tenant, "max_managed_users_per_user_admin", 50) if tenant else 50)
        )
        count_result = await session.execute(
            select(func.count(User.user_id)).where(
                User.manager_id == current_user.user_id,
                User.role == "user",
            )
        )
        count = count_result.scalar() or 0
        if count >= effective_limit:
            raise HTTPException(
                403,
                f"You have reached your limit of {effective_limit} users. Contact an admin to increase your limit.",
            )

    invite_token = generate_secure_token()
    manager_id = current_user.user_id if (current_user.role == "user_admin" and data.role == "user") else data.manager_id
    max_managed_val = data.max_managed_users if current_user.is_admin and data.max_managed_users is not None else None
    force_pw_change = auth_provider == "email"
    user = User(
        tenant_id=resolved_tenant_id,
        email=data.email.lower(),
        display_name=data.display_name.strip() or data.email.split("@")[0],
        phone=data.phone,
        department=data.department,
        timezone=data.timezone,
        manager_id=manager_id,
        employee_id=data.employee_id,
        hashed_password=None,
        role=data.role,
        auth_provider=auth_provider,
        invited_by=current_user.user_id,
        status="invited",
        force_password_change=force_pw_change,
        password_reset_token=invite_token,
        password_reset_expires=datetime.utcnow() + timedelta(days=7),
        max_products=data.max_products if current_user.is_admin and data.max_products is not None else None,
        max_managed_users=max_managed_val if data.role == "user_admin" else None,
    )
    session.add(user)
    await session.flush()

    await _log_audit(session, "user_invited", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=resolved_tenant_id,
                     target_type="user", target_id=user.user_id,
                     details={"email": user.email, "role": user.role})

    email_sent, email_error = await _send_invite_email(
        session, resolved_tenant_id,
        to_email=user.email,
        display_name=user.display_name or user.email,
        invite_token=invite_token,
    )
    return {"user": user.to_dict(), "email_sent": email_sent, "email_error": email_error}


@router.post("/users/add", status_code=201)
async def add_user(
    data: AddUserRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    if not can_provision_role(current_user.role, data.role):
        raise HTTPException(403, "You are not allowed to assign this role")
    if data.role == "zero_admin":
        await _assert_single_zero_admin(session)
    _require_valid_email_for_non_zero_admin((data.email or "").strip(), data.role)

    resolved_tenant_id = current_user.tenant_id or await _get_single_tenant_id(session)
    if not resolved_tenant_id:
        raise HTTPException(400, "No tenant configured. Configure organization in Settings first.")

    existing = await session.execute(select(User).where(User.email == data.email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Email already registered")

    auth_provider = (data.auth_provider or "email").lower()
    if auth_provider not in ("email", "ldap", "azure_ad"):
        raise HTTPException(400, "auth_provider must be email, ldap, or azure_ad")
    if auth_provider in ("ldap", "azure_ad") and data.role == "zero_admin":
        raise HTTPException(400, "Zero Admin cannot use LDAP or Azure AD")
    if data.role == "zero_admin":
        auth_provider = "email"
    await _validate_user_lob_manager(
        session,
        current_user,
        resolved_tenant_id,
        data.role,
        data.manager_id,
    )

    # Enforce max_managed_users when user_admin creates a user
    if current_user.role == "user_admin" and data.role == "user":
        creator = await session.get(User, current_user.user_id)
        tenant = await session.get(Tenant, resolved_tenant_id)
        effective_limit = (
            creator.max_managed_users
            if creator and creator.max_managed_users is not None
            else (getattr(tenant, "max_managed_users_per_user_admin", 50) if tenant else 50)
        )
        count_result = await session.execute(
            select(func.count(User.user_id)).where(
                User.manager_id == current_user.user_id,
                User.role == "user",
            )
        )
        count = count_result.scalar() or 0
        if count >= effective_limit:
            raise HTTPException(
                403,
                f"You have reached your limit of {effective_limit} users. Contact an admin to increase your limit.",
            )

    invite_token = generate_secure_token()
    manager_id = current_user.user_id if (current_user.role == "user_admin" and data.role == "user") else data.manager_id
    max_managed_val = data.max_managed_users if current_user.is_admin and data.max_managed_users is not None else None
    force_pw_change = auth_provider == "email"
    user = User(
        tenant_id=resolved_tenant_id,
        email=data.email.lower(),
        display_name=data.display_name.strip() or data.email.split("@")[0],
        phone=data.phone,
        department=data.department,
        timezone=data.timezone,
        manager_id=manager_id,
        employee_id=data.employee_id,
        hashed_password=None,
        role=data.role,
        auth_provider=auth_provider,
        invited_by=current_user.user_id,
        status="invited",
        force_password_change=force_pw_change,
        password_reset_token=invite_token,
        password_reset_expires=datetime.utcnow() + timedelta(days=7),
        max_products=data.max_products if current_user.is_admin and data.max_products is not None else None,
        max_managed_users=max_managed_val if data.role == "user_admin" else None,
    )
    session.add(user)
    await session.flush()

    await _log_audit(session, "user_added", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=resolved_tenant_id,
                     target_type="user", target_id=user.user_id,
                     details={"email": user.email, "role": user.role})

    email_sent, email_error = await _send_invite_email(
        session, resolved_tenant_id,
        to_email=user.email,
        display_name=user.display_name or user.email,
        invite_token=invite_token,
    )
    return {"user": user.to_dict(), "email_sent": email_sent, "email_error": email_error}


@router.post("/users/add-ldap", status_code=201)
async def add_ldap_user(
    data: AddLdapUserRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    """Add a user that authenticates via LDAP. Validates the username exists in LDAP before creating the record."""
    if not can_provision_role(current_user.role, data.role):
        raise HTTPException(403, "You are not allowed to assign this role")
    if data.role == "zero_admin":
        raise HTTPException(400, "Zero Admin cannot use LDAP")

    target_tenant_id = await _resolve_tenant_id(session, current_user, {"tenant_id": data.tenant_id} if data.tenant_id else None)

    tenant = await session.get(Tenant, target_tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if current_user.tenant_id and current_user.tenant_id != target_tenant_id:
        raise HTTPException(403, "Cannot add users to another tenant")
    await _validate_user_lob_manager(session, current_user, target_tenant_id, data.role, data.manager_id)

    if not tenant.ldap_config:
        raise HTTPException(400, "LDAP is not configured for this organization. Configure it in Settings → Organization.")

    from app.services.auth.ldap_auth import lookup_ldap_user
    ldap_result = await lookup_ldap_user(data.username.strip(), tenant.ldap_config)
    if not ldap_result:
        raise HTTPException(404, f"User '{data.username}' not found in LDAP")

    email = (ldap_result.email or f"{data.username}@ldap").strip().lower() or f"{data.username}@ldap"
    display_name = data.display_name or ldap_result.display_name or data.username
    _require_valid_email_for_non_zero_admin(email, data.role)

    existing = await session.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"User with email {email} already exists")

    # Enforce max_managed_users when user_admin creates a user
    if current_user.role == "user_admin" and data.role == "user":
        creator = await session.get(User, current_user.user_id)
        effective_limit = (
            creator.max_managed_users
            if creator and creator.max_managed_users is not None
            else getattr(tenant, "max_managed_users_per_user_admin", 50)
        )
        count_result = await session.execute(
            select(func.count(User.user_id)).where(
                User.manager_id == current_user.user_id,
                User.role == "user",
            )
        )
        count = count_result.scalar() or 0
        if count >= effective_limit:
            raise HTTPException(
                403,
                f"You have reached your limit of {effective_limit} users. Contact an admin to increase your limit.",
            )

    max_managed_val = data.max_managed_users if current_user.is_admin and data.max_managed_users is not None else None
    user = User(
        tenant_id=target_tenant_id,
        email=email,
        username=data.username,
        display_name=(display_name or "").strip() or data.username,
        phone=data.phone,
        department=data.department,
        timezone=data.timezone,
        manager_id=(current_user.user_id if (current_user.role == "user_admin" and data.role == "user") else data.manager_id),
        employee_id=data.employee_id,
        hashed_password=None,
        role=data.role,
        auth_provider="ldap",
        invited_by=current_user.user_id,
        status="active",
        max_products=data.max_products if current_user.is_admin and data.max_products is not None else None,
        max_managed_users=max_managed_val if data.role == "user_admin" else None,
    )
    session.add(user)
    await session.flush()

    await _log_audit(session, "user_added_ldap", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=target_tenant_id,
                     target_type="user", target_id=user.user_id,
                     details={"username": data.username, "email": user.email, "role": user.role})

    return {"user": user.to_dict()}


@router.patch("/users/{user_id}/role")
async def update_user_role(
    user_id: str,
    data: UpdateUserRoleRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role in ("admin", "user_admin"):
        raise HTTPException(403, "Only Zero Admin can change user roles")

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if current_user.tenant_id is not None and user.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")

    if data.role not in ("zero_admin", "admin", "user_admin", "user"):
        raise HTTPException(400, "Role must be zero_admin, admin, user_admin, or user")
    if not can_provision_role(current_user.role, data.role):
        raise HTTPException(403, "You are not allowed to assign this role")
    if data.role == "zero_admin":
        await _assert_single_zero_admin(session, exclude_user_id=user_id)

    old_role = user.role
    user.role = data.role

    await _log_audit(session, "role_changed", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id,
                     target_type="user", target_id=user_id,
                     details={"old_role": old_role, "new_role": data.role})

    return user.to_dict()


@router.patch("/users/{user_id}/max-products")
async def update_user_max_products(
    user_id: str,
    data: UpdateUserMaxProductsRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Admin only: set how many products a user_admin can create. Null = use tenant default."""
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if current_user.tenant_id is not None and user.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")

    user.max_products = data.max_products

    await _log_audit(session, "max_products_updated", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id,
                     target_type="user", target_id=user_id,
                     details={"max_products": data.max_products})

    return user.to_dict()


@router.patch("/users/{user_id}/max-managed-users")
async def update_user_max_managed_users(
    user_id: str,
    data: UpdateUserMaxManagedUsersRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Admin only: set how many users a user_admin can create. Null = use tenant default."""
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if current_user.tenant_id is not None and user.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")
    if user.role != "user_admin":
        raise HTTPException(400, "max_managed_users only applies to User Admin role")

    user.max_managed_users = data.max_managed_users

    await _log_audit(session, "max_managed_users_updated", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id,
                     target_type="user", target_id=user_id,
                     details={"max_managed_users": data.max_managed_users})

    return user.to_dict()


@router.patch("/users/{user_id}/deactivate")
async def deactivate_user(
    user_id: str,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    if user_id == current_user.user_id:
        raise HTTPException(400, "Cannot deactivate yourself")

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if current_user.tenant_id is not None and user.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")
    _ensure_user_admin_lob_access(current_user, user)

    user.is_active = False
    user.status = "disabled"

    # Revoke all active sessions
    result = await session.execute(
        select(UserSession).where(UserSession.user_id == user_id, UserSession.revoked == False)
    )
    sessions_revoked = 0
    for s in result.scalars().all():
        s.revoked = True
        sessions_revoked += 1

    # Revoke all product access
    from app.models.product_access import ProductAccess
    from sqlalchemy import delete as sql_delete
    pa_result = await session.execute(
        sql_delete(ProductAccess).where(ProductAccess.user_id == user_id)
    )
    access_revoked = pa_result.rowcount

    # Clear any pending invite tokens
    user.password_reset_token = None
    user.password_reset_expires = None

    await _log_audit(session, "user_deactivated", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id,
                     target_type="user", target_id=user_id,
                     details={"sessions_revoked": sessions_revoked, "access_revoked": access_revoked})

    return {"message": "User deactivated", "sessions_revoked": sessions_revoked, "access_revoked": access_revoked}


@router.patch("/users/{user_id}/activate")
async def activate_user(
    user_id: str,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if current_user.tenant_id is not None and user.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")
    _ensure_user_admin_lob_access(current_user, user)
    user.is_active = True
    user.status = "active"
    await _log_audit(session, "user_activated", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id, target_type="user", target_id=user_id)
    return {"message": "User activated"}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    """Permanently delete a user and their sessions/access. Cannot delete yourself."""
    if user_id == current_user.user_id:
        raise HTTPException(400, "Cannot delete yourself")

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    # Tenant-scoped: same tenant required, unless current user is global admin (no tenant)
    if current_user.tenant_id is not None and user.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")
    _ensure_user_admin_lob_access(current_user, user)

    from sqlalchemy import delete as sql_del, update as sql_upd
    from app.models.product_access import ProductAccess
    from app.models.product import Product

    await session.execute(sql_del(UserSession).where(UserSession.user_id == user_id))
    await session.execute(sql_del(ProductAccess).where(ProductAccess.user_id == user_id))
    await session.execute(sql_del(PasswordHistory).where(PasswordHistory.user_id == user_id))
    await session.execute(sql_upd(Product).where(Product.created_by == user_id).values(created_by=None))
    await session.delete(user)

    await _log_audit(session, "user_deleted", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id,
                     target_type="user", target_id=user_id,
                     details={"email": user.email})

    return {"message": "User deleted"}


# ── Tenant Auth Config (Admin only; global admin can configure any tenant) ──


# ── Bulk Operations ───────────────────────────────────────────────────


@router.post("/users/bulk-action")
async def bulk_user_action(
    data: dict,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    """Perform bulk operations on selected users."""
    action = data.get("action")
    user_ids = data.get("user_ids", [])
    if not action or not user_ids:
        raise HTTPException(400, "action and user_ids are required")
    if action not in ("deactivate", "activate", "delete", "resend_invite"):
        raise HTTPException(400, "Invalid action")

    results = {"success": 0, "failed": 0, "errors": []}
    for uid in user_ids:
        try:
            target = await session.get(User, uid)
            if not target:
                results["failed"] += 1
                results["errors"].append(f"{uid}: not found")
                continue
            if current_user.tenant_id and target.tenant_id != current_user.tenant_id:
                results["failed"] += 1
                continue
            if current_user.role == "user_admin":
                if target.role != "user" or target.manager_id != current_user.user_id:
                    results["failed"] += 1
                    continue

            if action == "deactivate":
                if uid == current_user.user_id:
                    results["errors"].append(f"{uid}: cannot deactivate yourself")
                    results["failed"] += 1
                    continue
                target.is_active = False
                target.status = "disabled"
                res = await session.execute(
                    select(UserSession).where(UserSession.user_id == uid, UserSession.revoked == False)
                )
                for s in res.scalars().all():
                    s.revoked = True
            elif action == "activate":
                target.is_active = True
                target.status = "active"
            elif action == "delete":
                if uid == current_user.user_id:
                    results["errors"].append(f"{uid}: cannot delete yourself")
                    results["failed"] += 1
                    continue
                from sqlalchemy import delete as sql_delete
                from app.models.product_access import ProductAccess
                await session.execute(sql_delete(UserSession).where(UserSession.user_id == uid))
                await session.execute(sql_delete(ProductAccess).where(ProductAccess.user_id == uid))
                await session.execute(sql_delete(PasswordHistory).where(PasswordHistory.user_id == uid))
                await session.delete(target)
            elif action == "resend_invite":
                if target.status != "invited":
                    results["errors"].append(f"{uid}: not in invited status")
                    results["failed"] += 1
                    continue
                invite_token = generate_secure_token()
                target.password_reset_token = invite_token
                target.password_reset_expires = datetime.utcnow() + timedelta(days=7)
                await _send_invite_email(
                    session, target.tenant_id,
                    to_email=target.email,
                    display_name=target.display_name or target.email,
                    invite_token=invite_token,
                )

            results["success"] += 1
            await _log_audit(session, f"bulk_{action}", request,
                             actor_id=current_user.user_id, actor_email=current_user.email,
                             tenant_id=current_user.tenant_id,
                             target_type="user", target_id=uid)
        except Exception as exc:
            results["failed"] += 1
            results["errors"].append(f"{uid}: {str(exc)}")

    return results


# ── User Stats (Admin Dashboard) ─────────────────────────────────────


@router.get("/stats")
async def user_stats(
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Aggregated user statistics for admin dashboard."""
    tid = current_user.tenant_id
    base = select(User)
    if tid:
        base = base.where(User.tenant_id == tid)

    result = await session.execute(base)
    users = result.scalars().all()

    by_role = {}
    by_auth = {}
    by_status = {}
    recent_logins_24h = 0
    inactive_30d = 0
    now = datetime.utcnow()

    for u in users:
        by_role[u.role] = by_role.get(u.role, 0) + 1
        by_auth[u.auth_provider] = by_auth.get(u.auth_provider, 0) + 1
        by_status[u.status] = by_status.get(u.status, 0) + 1
        if u.last_login_at and (now - u.last_login_at).days < 1:
            recent_logins_24h += 1
        if not u.last_login_at or (now - u.last_login_at).days >= 30:
            inactive_30d += 1

    failed_q = (
        select(func.count(AuditLog.id))
        .where(AuditLog.action == "login_failed")
        .where(AuditLog.timestamp > (now - timedelta(hours=24)))
    )
    if tid:
        failed_q = failed_q.where(AuditLog.tenant_id == tid)
    failed_24h = (await session.execute(failed_q)).scalar() or 0

    return {
        "total_users": len(users),
        "by_role": by_role,
        "by_auth_method": by_auth,
        "by_status": by_status,
        "recent_logins_24h": recent_logins_24h,
        "failed_logins_24h": failed_24h,
        "inactive_30d": inactive_30d,
    }


# ── User-Specific Audit Logs ─────────────────────────────────────────


@router.get("/users/{user_id}/audit")
async def user_audit_log(
    user_id: str,
    limit: int = 50,
    offset: int = 0,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    """Get audit log entries for a specific user."""
    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if current_user.tenant_id and target.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")
    if current_user.role == "user_admin":
        _ensure_user_admin_lob_access(current_user, target)

    query = (
        select(AuditLog)
        .where(
            (AuditLog.actor_user_id == user_id) | (AuditLog.target_id == user_id)
        )
        .order_by(AuditLog.timestamp.desc())
        .limit(limit).offset(offset)
    )
    result = await session.execute(query)
    return [log.to_dict() for log in result.scalars().all()]


# ── User Sessions Management ─────────────────────────────────────────


@router.get("/users/{user_id}/sessions")
async def list_user_sessions(
    user_id: str,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    """List active sessions for a user."""
    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if current_user.tenant_id and target.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")
    if current_user.role == "user_admin":
        _ensure_user_admin_lob_access(current_user, target)

    result = await session.execute(
        select(UserSession).where(
            UserSession.user_id == user_id, UserSession.revoked == False
        ).order_by(UserSession.created_at.desc())
    )
    sessions = result.scalars().all()
    return [
        {
            "session_id": s.session_id,
            "ip_address": s.ip_address,
            "user_agent": s.user_agent,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
        }
        for s in sessions
    ]


@router.delete("/users/{user_id}/sessions/{session_id}")
async def revoke_user_session(
    user_id: str,
    session_id: str,
    request: Request,
    current_user: CurrentUser = Depends(require_role("user_admin")),
    session: AsyncSession = Depends(get_session),
):
    """Revoke a specific session for a user."""
    result = await session.execute(
        select(UserSession).where(
            UserSession.session_id == session_id,
            UserSession.user_id == user_id,
            UserSession.revoked == False,
        )
    )
    us = result.scalar_one_or_none()
    if not us:
        raise HTTPException(404, "Session not found")
    us.revoked = True
    await _log_audit(session, "session_revoked", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id, target_type="session", target_id=session_id)
    return {"message": "Session revoked"}


# ── Force Logout ──────────────────────────────────────────────────────


@router.post("/users/{user_id}/force-logout")
async def force_logout_user(
    user_id: str,
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Revoke all sessions for a user, forcing them to re-authenticate."""
    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if current_user.tenant_id and target.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found")

    result = await session.execute(
        select(UserSession).where(UserSession.user_id == user_id, UserSession.revoked == False)
    )
    count = 0
    for s in result.scalars().all():
        s.revoked = True
        count += 1

    await _log_audit(session, "force_logout", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id,
                     target_type="user", target_id=user_id,
                     details={"sessions_revoked": count})
    return {"message": f"User logged out ({count} sessions revoked)"}


# ── Tenant Auth Config (Admin only; global admin can configure any tenant) ──


@router.get("/tenants")
async def list_tenants(
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """List all tenants (global admin only). Tenant admins see only their tenant."""
    query = select(Tenant).order_by(Tenant.name)
    if current_user.tenant_id:
        query = query.where(Tenant.tenant_id == current_user.tenant_id)
    result = await session.execute(query)
    tenants = result.scalars().all()
    return [{"tenant_id": t.tenant_id, "name": t.name, "domain": t.domain, "auth_method": t.auth_method} for t in tenants]


@router.get("/tenant/auth")
async def get_tenant_auth(
    tenant_id: Optional[str] = None,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Get tenant's auth config. Global admin must pass tenant_id. Tenant admin uses own tenant."""
    target_tenant_id = await _resolve_tenant_id(session, current_user, {"tenant_id": tenant_id} if tenant_id else None)
    tenant = await session.get(Tenant, target_tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if current_user.tenant_id and current_user.tenant_id != target_tenant_id:
        raise HTTPException(403, "Cannot access another tenant's config")
    out = {
        "auth_method": tenant.auth_method,
        "domain": tenant.domain,
        "ldap_require_ssl": getattr(tenant, "ldap_require_ssl", False),
        "max_products_per_user_admin": tenant.max_products_per_user_admin,
        "max_managed_users_per_user_admin": getattr(tenant, "max_managed_users_per_user_admin", 50),
    }
    if tenant.ldap_config:
        cfg = dict(tenant.ldap_config)
        if cfg.get("bind_password"):
            cfg["bind_password"] = "********"
        out["ldap_config"] = cfg
    else:
        out["ldap_config"] = None
    out["smtp_config"] = _mask_smtp_config(getattr(tenant, "smtp_config", None))
    out["password_policy"] = {**DEFAULT_PASSWORD_POLICY, **(tenant.password_policy or {})}
    if tenant.azure_ad_config:
        az_cfg = dict(tenant.azure_ad_config)
        if az_cfg.get("client_secret"):
            az_cfg["client_secret"] = "********"
        out["azure_ad_config"] = az_cfg
    else:
        out["azure_ad_config"] = None
    out["google_config"] = tenant.google_config
    return out


@router.patch("/tenant/auth")
async def update_tenant_auth(
    data: dict,
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Update tenant's auth config. Global admin must pass tenant_id. Tenant admin uses own tenant."""
    target_tenant_id = await _resolve_tenant_id(session, current_user, data)
    tenant = await session.get(Tenant, target_tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if current_user.tenant_id and current_user.tenant_id != target_tenant_id:
        raise HTTPException(403, "Cannot update another tenant's config")

    if "auth_method" in data:
        tenant.auth_method = data["auth_method"]
    if "domain" in data:
        tenant.domain = data["domain"].strip().lower() if data["domain"] else None
    if "ldap_config" in data:
        new_cfg = data["ldap_config"]
        if isinstance(new_cfg, dict):
            # Preserve existing bind_password if frontend sends placeholder
            if tenant.ldap_config and new_cfg.get("bind_password") in ("********", "", None):
                new_cfg = {**new_cfg, "bind_password": tenant.ldap_config.get("bind_password", "")}
            tenant.ldap_config = new_cfg
    if "ldap_require_ssl" in data:
        tenant.ldap_require_ssl = bool(data["ldap_require_ssl"])
    if "smtp_config" in data:
        new_smtp = data["smtp_config"]
        if isinstance(new_smtp, dict):
            if tenant.smtp_config and new_smtp.get("password") in ("********", "", None):
                new_smtp = {**new_smtp, "password": tenant.smtp_config.get("password", "")}
            tenant.smtp_config = new_smtp
    if "azure_ad_config" in data:
        new_az = data["azure_ad_config"]
        if isinstance(new_az, dict):
            if tenant.azure_ad_config and new_az.get("client_secret") in ("********", "", None):
                new_az = {**new_az, "client_secret": tenant.azure_ad_config.get("client_secret", "")}
            tenant.azure_ad_config = new_az
        else:
            tenant.azure_ad_config = new_az
    if "google_config" in data:
        tenant.google_config = data["google_config"]
    if "mfa_required" in data:
        tenant.mfa_required = data["mfa_required"]
    if "session_timeout_minutes" in data:
        tenant.session_timeout_minutes = data["session_timeout_minutes"]
    if "max_products_per_user_admin" in data:
        tenant.max_products_per_user_admin = int(data["max_products_per_user_admin"])
    if "max_managed_users_per_user_admin" in data:
        tenant.max_managed_users_per_user_admin = int(data["max_managed_users_per_user_admin"])
    if "ip_allowlist" in data:
        tenant.ip_allowlist = data["ip_allowlist"] if isinstance(data["ip_allowlist"], list) else None
    if "ip_denylist" in data:
        tenant.ip_denylist = data["ip_denylist"] if isinstance(data["ip_denylist"], list) else None
    if "password_policy" in data and isinstance(data["password_policy"], dict):
        current_policy = dict(tenant.password_policy or {})
        current_policy.update(data["password_policy"])
        tenant.password_policy = current_policy

    await _log_audit(session, "auth_config_changed", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=tenant.tenant_id, target_type="tenant",
                     target_id=tenant.tenant_id,
                     details={"auth_method": tenant.auth_method})

    return tenant.to_dict()


@router.post("/tenant/auth/test-smtp")
async def test_smtp(
    data: dict,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    target_tenant_id = await _resolve_tenant_id(session, current_user, data)
    tenant = await session.get(Tenant, target_tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if current_user.tenant_id and current_user.tenant_id != target_tenant_id:
        raise HTTPException(403, "Cannot test another tenant's SMTP config")
    to_email = (data.get("to_email") or "").strip().lower()
    if not to_email:
        raise HTTPException(400, "to_email is required")
    smtp_config = data.get("smtp_config") if isinstance(data.get("smtp_config"), dict) else tenant.smtp_config
    if not smtp_config:
        raise HTTPException(400, "SMTP config is not set")
    if tenant.smtp_config and smtp_config.get("password") in ("********", "", None):
        smtp_config = {**smtp_config, "password": tenant.smtp_config.get("password", "")}
    try:
        await to_thread(
            send_smtp_email,
            smtp_config,
            to_email=to_email,
            subject="ReTrace SMTP test email",
            html_body="<p>This is a test email from ReTrace SMTP settings.</p>",
            text_body="This is a test email from ReTrace SMTP settings.",
        )
        return {"ok": True, "message": "SMTP test email sent"}
    except Exception as exc:
        raise HTTPException(400, f"SMTP test failed: {exc}")


@router.post("/tenant/auth/test-ldap")
async def test_ldap(
    data: dict,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Test LDAP connectivity using the provided configuration."""
    ldap_config = data.get("ldap_config", {})
    if not ldap_config or not isinstance(ldap_config, dict):
        raise HTTPException(400, "ldap_config is required")
    if not ldap_config.get("host"):
        raise HTTPException(400, "LDAP host is required")

    from app.services.auth.ldap_auth import test_ldap_connection
    result = await test_ldap_connection(ldap_config, raw_password=True)
    if not result["success"]:
        raise HTTPException(400, result["message"])
    return result


@router.post("/tenant/auth/test-azure-ad")
async def test_azure_ad(
    data: dict,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Validate Azure AD config by attempting a client-credentials token request."""
    target_tenant_id = await _resolve_tenant_id(session, current_user, data)
    tenant = await session.get(Tenant, target_tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if current_user.tenant_id and current_user.tenant_id != target_tenant_id:
        raise HTTPException(403, "Cannot test another tenant's Azure AD config")

    azure_config = data.get("azure_ad_config") if isinstance(data.get("azure_ad_config"), dict) else tenant.azure_ad_config
    if not azure_config:
        raise HTTPException(400, "Azure AD config is not set")

    client_id = azure_config.get("client_id", "")
    client_secret = azure_config.get("client_secret", "")
    az_tenant_id = azure_config.get("tenant_id", "common")

    if not client_id:
        raise HTTPException(400, "client_id is required")
    if not client_secret or client_secret == "********":
        if tenant.azure_ad_config and tenant.azure_ad_config.get("client_secret"):
            client_secret = tenant.azure_ad_config["client_secret"]
        else:
            raise HTTPException(400, "client_secret is required")

    import httpx
    token_url = f"https://login.microsoftonline.com/{az_tenant_id}/oauth2/v2.0/token"
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(token_url, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            })
        if resp.status_code == 200:
            return {"success": True, "message": "Azure AD credentials validated successfully."}
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        error_desc = body.get("error_description", resp.text[:200])
        return {"success": False, "message": f"Azure AD returned {resp.status_code}: {error_desc}"}
    except Exception as exc:
        raise HTTPException(400, f"Azure AD connection test failed: {exc}")


# ── Product Access ────────────────────────────────────────────────────


@router.get("/products/{product_id}/access")
async def list_product_access(
    product_id: str,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    from app.models.product_access import ProductAccess
    result = await session.execute(
        select(ProductAccess).where(ProductAccess.product_id == product_id)
    )
    return [a.to_dict() for a in result.scalars().all()]


@router.post("/products/{product_id}/access", status_code=201)
async def grant_product_access(
    product_id: str,
    data: dict,
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    from app.models.product_access import ProductAccess
    user_id = data.get("user_id")
    permission = data.get("permission", "view")

    if not user_id:
        raise HTTPException(400, "user_id is required")

    target_user = await session.get(User, user_id)
    if not target_user or target_user.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "User not found in your organization")

    existing = await session.execute(
        select(ProductAccess).where(
            ProductAccess.product_id == product_id,
            ProductAccess.user_id == user_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "User already has access")

    access = ProductAccess(
        product_id=product_id,
        user_id=user_id,
        permission=permission,
        granted_by=current_user.user_id,
    )
    session.add(access)

    await _log_audit(session, "access_granted", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id,
                     target_type="product", target_id=product_id,
                     details={"user_id": user_id, "permission": permission})

    await session.flush()
    return access.to_dict()


@router.delete("/products/{product_id}/access/{user_id}")
async def revoke_product_access(
    product_id: str,
    user_id: str,
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    from app.models.product_access import ProductAccess
    result = await session.execute(
        select(ProductAccess).where(
            ProductAccess.product_id == product_id,
            ProductAccess.user_id == user_id,
        )
    )
    access = result.scalar_one_or_none()
    if not access:
        raise HTTPException(404, "Access record not found")

    await session.delete(access)

    await _log_audit(session, "access_revoked", request,
                     actor_id=current_user.user_id, actor_email=current_user.email,
                     tenant_id=current_user.tenant_id,
                     target_type="product", target_id=product_id,
                     details={"user_id": user_id})

    return {"message": "Access revoked"}


# ── Audit Log (Tenant Admin view) ────────────────────────────────────


@router.get("/audit")
async def list_audit_logs(
    limit: int = 50,
    offset: int = 0,
    action: Optional[str] = None,
    actor_id: Optional[str] = None,
    target_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """List audit logs with optional filters."""
    query = select(AuditLog).where(AuditLog.tenant_id == current_user.tenant_id)
    count_q = select(func.count(AuditLog.id)).where(AuditLog.tenant_id == current_user.tenant_id)

    if action:
        query = query.where(AuditLog.action == action)
        count_q = count_q.where(AuditLog.action == action)
    if actor_id:
        query = query.where(AuditLog.actor_user_id == actor_id)
        count_q = count_q.where(AuditLog.actor_user_id == actor_id)
    if target_id:
        query = query.where(AuditLog.target_id == target_id)
        count_q = count_q.where(AuditLog.target_id == target_id)
    if date_from:
        try:
            df = datetime.fromisoformat(date_from)
            query = query.where(AuditLog.timestamp >= df)
            count_q = count_q.where(AuditLog.timestamp >= df)
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.fromisoformat(date_to)
            query = query.where(AuditLog.timestamp <= dt)
            count_q = count_q.where(AuditLog.timestamp <= dt)
        except ValueError:
            pass

    total = (await session.execute(count_q)).scalar() or 0
    query = query.order_by(AuditLog.timestamp.desc()).limit(limit).offset(offset)
    result = await session.execute(query)
    return {
        "logs": [log.to_dict() for log in result.scalars().all()],
        "total": total,
    }


# ── Enterprise Auth: LDAP ─────────────────────────────────────────────


class LDAPLoginRequest(BaseModel):
    username: str = Field(...)
    password: str = Field(...)
    tenant_domain: str = Field(...)


async def _try_local_auth(
    session: AsyncSession,
    tenant_id: str,
    username: str,
    password: str,
    tenant_domain: str,
) -> Optional[User]:
    """Try to authenticate against local DB users in the tenant. Returns User if success."""
    username_lower = username.strip().lower()
    candidates = [username_lower]
    if "@" not in username_lower and tenant_domain:
        candidates.append(f"{username_lower}@{tenant_domain}")
    for email_candidate in candidates:
        result = await session.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.email == email_candidate,
                User.is_active == True,
            )
        )
        u = result.scalar_one_or_none()
        if u and u.hashed_password and verify_password(password, u.hashed_password):
            return u
    return None


@router.post("/ldap", response_model=TokenResponse)
async def login_ldap(
    data: LDAPLoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Authenticate via LDAP or fallback to local DB. Admin can always use local auth."""
    result = await session.execute(select(Tenant).where(Tenant.domain == data.tenant_domain.lower().strip()))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(400, "Unknown organization domain")

    user = None

    if tenant.ldap_config:
        from app.services.auth.ldap_auth import authenticate_ldap
        ldap_result = await authenticate_ldap(data.username, data.password, tenant.ldap_config)
        if ldap_result:
            email = (ldap_result.email or f"{data.username}@ldap").strip().lower() or f"{data.username}@ldap"
            user_result = await session.execute(select(User).where(User.email == email))
            user = user_result.scalar_one_or_none()
            if not user:
                user = User(
                    tenant_id=tenant.tenant_id,
                    email=email,
                    display_name=ldap_result.display_name,
                    auth_provider="ldap",
                    role="user",
                )
                session.add(user)
                await session.flush()
                logger.info("ldap_jit_provision", email=email, tenant_id=tenant.tenant_id)

    if not user:
        user = await _try_local_auth(
            session, tenant.tenant_id, data.username, data.password, data.tenant_domain
        )
        if user:
            logger.info("ldap_fallback_local", email=user.email, tenant_id=tenant.tenant_id)

    if not user:
        await _log_audit(session, "login_failed", request,
                         actor_email=data.username, tenant_id=tenant.tenant_id,
                         details={"reason": "ldap_and_local_failed", "provider": "ldap"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is deactivated")

    provider = "ldap" if user.auth_provider == "ldap" else "email"
    await _log_audit(session, "login", request, actor_id=user.user_id, actor_email=user.email,
                     tenant_id=tenant.tenant_id,
                     details={"provider": provider})

    return await _create_session_and_tokens(user, request, session)


# ── Enterprise Auth: Azure AD ────────────────────────────────────────


class OAuthCallbackRequest(BaseModel):
    code: str = Field(...)
    tenant_domain: str = Field(...)


@router.post("/azure-ad", response_model=TokenResponse)
async def login_azure_ad(
    data: OAuthCallbackRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Exchange Azure AD authorization code for app tokens."""
    result = await session.execute(select(Tenant).where(Tenant.domain == data.tenant_domain.lower()))
    tenant = result.scalar_one_or_none()
    if not tenant or not tenant.azure_ad_config:
        raise HTTPException(400, "Azure AD not configured for this domain")

    from app.services.auth.azure_ad_auth import exchange_code_for_user
    azure_user = await exchange_code_for_user(data.code, tenant.azure_ad_config)
    if not azure_user:
        await _log_audit(session, "login_failed", request,
                         tenant_id=tenant.tenant_id,
                         details={"reason": "azure_ad_code_exchange_failed", "provider": "azure_ad"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Azure AD authentication failed")

    # JIT provisioning
    user_result = await session.execute(select(User).where(User.email == azure_user.email))
    user = user_result.scalar_one_or_none()
    if not user:
        user = User(
            tenant_id=tenant.tenant_id,
            email=azure_user.email,
            display_name=azure_user.display_name,
            auth_provider="azure_ad",
            role="user",
        )
        session.add(user)
        await session.flush()
        logger.info("azure_ad_jit_provision", email=azure_user.email)
    elif getattr(user, "status", None) == "invited":
        user.status = "active"
        user.password_reset_token = None
        user.password_reset_expires = None
        user.force_password_change = False

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is deactivated")

    await _log_audit(session, "login", request, actor_id=user.user_id,
                     actor_email=azure_user.email, tenant_id=tenant.tenant_id,
                     details={"provider": "azure_ad"})

    return await _create_session_and_tokens(user, request, session)


@router.get("/azure-ad/authorize")
async def azure_ad_authorize_url(
    domain: str,
    session: AsyncSession = Depends(get_session),
):
    """Get the Azure AD authorization URL for a given tenant domain."""
    result = await session.execute(select(Tenant).where(Tenant.domain == domain.lower()))
    tenant = result.scalar_one_or_none()
    if not tenant or not tenant.azure_ad_config:
        raise HTTPException(400, "Azure AD not configured for this domain")
    from app.services.auth.azure_ad_auth import get_authorize_url
    return {"authorize_url": get_authorize_url(tenant.azure_ad_config)}


# ── Enterprise Auth: Google ──────────────────────────────────────────


@router.post("/google", response_model=TokenResponse)
async def login_google(
    data: OAuthCallbackRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Exchange Google authorization code for app tokens."""
    result = await session.execute(select(Tenant).where(Tenant.domain == data.tenant_domain.lower()))
    tenant = result.scalar_one_or_none()
    if not tenant or not tenant.google_config:
        raise HTTPException(400, "Google auth not configured for this domain")

    from app.services.auth.google_auth import exchange_code_for_user
    google_user = await exchange_code_for_user(data.code, tenant.google_config)
    if not google_user:
        await _log_audit(session, "login_failed", request,
                         tenant_id=tenant.tenant_id,
                         details={"reason": "google_code_exchange_failed", "provider": "google"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google authentication failed")

    # JIT provisioning
    user_result = await session.execute(select(User).where(User.email == google_user.email))
    user = user_result.scalar_one_or_none()
    if not user:
        user = User(
            tenant_id=tenant.tenant_id,
            email=google_user.email,
            display_name=google_user.display_name,
            auth_provider="google",
            role="user",
        )
        session.add(user)
        await session.flush()
        logger.info("google_jit_provision", email=google_user.email)

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is deactivated")

    await _log_audit(session, "login", request, actor_id=user.user_id,
                     actor_email=google_user.email, tenant_id=tenant.tenant_id,
                     details={"provider": "google"})

    return await _create_session_and_tokens(user, request, session)


@router.get("/google/authorize")
async def google_authorize_url(
    domain: str,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Tenant).where(Tenant.domain == domain.lower()))
    tenant = result.scalar_one_or_none()
    if not tenant or not tenant.google_config:
        raise HTTPException(400, "Google auth not configured for this domain")
    from app.services.auth.google_auth import get_authorize_url
    return {"authorize_url": get_authorize_url(tenant.google_config)}
