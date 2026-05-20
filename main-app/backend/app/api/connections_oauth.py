"""OAuth flows for cloud-storage source connections (Dropbox, Box, Drive).

Two endpoints per provider:
  GET /oauth/init/<provider>?name=<display>   (auth required)
  GET /oauth/callback/<provider>?code=&state= (public; state is HMAC-signed)

The init endpoint builds the consent URL and returns it as JSON. The frontend
opens it in a popup. After the user authorises, the provider redirects the
popup to /oauth/callback/<provider>, which exchanges the code for tokens,
creates the Connection row, and returns a small HTML page that closes the
popup. The parent window detects the close and refreshes its connections list.

State token format: <base64-payload>.<hex-hmac>
  payload = json({tenant_id, user_id, name, exp, nonce})
  hmac    = HMAC-SHA256(JWT_SECRET, payload)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime
from typing import Optional, Tuple
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import get_current_user, CurrentUser
from app.db.database import get_session
from app.models.connection import Connection
from app.services.sources import encrypt_config

router = APIRouter()

_STATE_TTL = 600  # seconds


# ─── State helpers ──────────────────────────────────────────────────


def _sign(payload: bytes) -> str:
    sig = hmac.new(
        settings.JWT_SECRET.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return f"{base64.urlsafe_b64encode(payload).decode('ascii')}.{sig}"


def _verify(token: str) -> dict:
    try:
        b64, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(b64.encode("ascii"))
    except Exception:
        raise HTTPException(400, "Invalid state token")
    expected = hmac.new(
        settings.JWT_SECRET.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(400, "State signature mismatch")
    data = json.loads(payload)
    if data.get("exp", 0) < int(time.time()):
        raise HTTPException(400, "OAuth state expired — try again")
    return data


def _make_state(tenant_id: Optional[str], user_id: str, name: str) -> str:
    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "name": name,
            "exp": int(time.time()) + _STATE_TTL,
            "nonce": secrets.token_urlsafe(16),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return _sign(payload)


# ─── Provider config ────────────────────────────────────────────────


def _provider_config(provider: str) -> Tuple[str, str, str, str, str, str, dict]:
    """Returns (client_id, client_secret, redirect_uri, auth_url, token_url, scope, extra_auth_params)."""
    if provider == "dropbox":
        if not (settings.DROPBOX_CLIENT_ID and settings.DROPBOX_CLIENT_SECRET):
            raise HTTPException(503, "Dropbox OAuth is not configured on this server")
        return (
            settings.DROPBOX_CLIENT_ID,
            settings.DROPBOX_CLIENT_SECRET,
            settings.DROPBOX_REDIRECT_URI
            or "http://localhost:8000/api/v1/connections/oauth/callback/dropbox",
            "https://www.dropbox.com/oauth2/authorize",
            "https://api.dropboxapi.com/oauth2/token",
            "files.metadata.read files.content.read account_info.read",
            # Dropbox: offline access for refresh tokens.
            {"token_access_type": "offline"},
        )
    if provider == "google_drive":
        if not (settings.GOOGLE_DRIVE_CLIENT_ID and settings.GOOGLE_DRIVE_CLIENT_SECRET):
            raise HTTPException(503, "Google Drive OAuth is not configured on this server")
        return (
            settings.GOOGLE_DRIVE_CLIENT_ID,
            settings.GOOGLE_DRIVE_CLIENT_SECRET,
            settings.GOOGLE_DRIVE_REDIRECT_URI
            or "http://localhost:8000/api/v1/connections/oauth/callback/google_drive",
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://oauth2.googleapis.com/token",
            "https://www.googleapis.com/auth/drive.file",
            # Google: access_type=offline + prompt=consent force a refresh_token
            # to be returned on every consent (without prompt=consent, repeat
            # users of the same client don't get a fresh refresh_token).
            {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
        )
    if provider == "box":
        if not (settings.BOX_CLIENT_ID and settings.BOX_CLIENT_SECRET):
            raise HTTPException(503, "Box OAuth is not configured on this server")
        return (
            settings.BOX_CLIENT_ID,
            settings.BOX_CLIENT_SECRET,
            settings.BOX_REDIRECT_URI
            or "http://localhost:8000/api/v1/connections/oauth/callback/box",
            "https://account.box.com/api/oauth2/authorize",
            "https://api.box.com/oauth2/token",
            # Scopes are configured per-app in Box Developer Console; the OAuth
            # init URL doesn't need to pass them. Empty string is fine — Box
            # accepts requests without a `scope` param.
            "",
            # Box returns a refresh_token by default; no extra params needed.
            {},
        )
    raise HTTPException(404, f"Unsupported OAuth provider: {provider}")


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/oauth/init/{provider}")
async def oauth_init(
    provider: str,
    name: str = Query(..., min_length=1, max_length=255),
    current_user: CurrentUser = Depends(get_current_user),
):
    client_id, _, redirect_uri, auth_url, _, scope, extra = _provider_config(provider)
    state = _make_state(current_user.tenant_id, current_user.user_id, name)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        **extra,
    }
    # Some providers (e.g. Box) configure scopes at the app level and don't
    # want a scope param in the URL. Only include it when non-empty.
    if scope:
        params["scope"] = scope
    return {"auth_url": f"{auth_url}?{urlencode(params)}"}


@router.get("/oauth/callback/{provider}", response_class=HTMLResponse)
async def oauth_callback(
    provider: str,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    if error:
        return _popup_html(False, error_description or error)
    if not code or not state:
        return _popup_html(False, "Missing code or state in callback")

    try:
        meta = _verify(state)
    except HTTPException as e:
        return _popup_html(False, e.detail)

    client_id, client_secret, redirect_uri, _, token_url, _, _ = _provider_config(provider)

    # Exchange the authorisation code for tokens
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                token_url,
                data={
                    "code": code,
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                },
            )
        if resp.status_code != 200:
            return _popup_html(False, f"Token exchange failed: {resp.text}")
        tok = resp.json()
    except Exception as e:
        return _popup_html(False, f"Token exchange error: {e}")

    refresh_token = tok.get("refresh_token")
    if not refresh_token:
        return _popup_html(
            False,
            "Provider didn't return a refresh_token. Re-create the OAuth app "
            "with offline access enabled.",
        )

    # Pull account info so we can label the connection.
    account_email = ""
    if provider == "dropbox":
        try:
            from dropbox import Dropbox
            tmp = Dropbox(
                oauth2_refresh_token=refresh_token,
                app_key=client_id,
                app_secret=client_secret,
            )
            acct = tmp.users_get_current_account()
            account_email = getattr(acct, "email", "")
            tmp.close()
        except Exception:
            pass
    elif provider == "google_drive":
        # Google returns an access_token in the same response — use it to
        # call userinfo without needing the full SDK roundtrip.
        access_token = tok.get("access_token")
        if access_token:
            try:
                async with httpx.AsyncClient(timeout=10) as http:
                    info = await http.get(
                        "https://www.googleapis.com/oauth2/v2/userinfo",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                if info.status_code == 200:
                    account_email = info.json().get("email", "") or ""
            except Exception:
                pass
    elif provider == "box":
        # Box returns access_token in the same response; call /users/me to get
        # the account's login email for connection labelling.
        access_token = tok.get("access_token")
        if access_token:
            try:
                async with httpx.AsyncClient(timeout=10) as http:
                    info = await http.get(
                        "https://api.box.com/2.0/users/me",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                if info.status_code == 200:
                    body = info.json()
                    account_email = body.get("login", "") or body.get("name", "") or ""
            except Exception:
                pass

    # Box's refresh_token rotates on every refresh, so we also persist the
    # initial access_token to skip a round-trip on the first request.
    config = {"refresh_token": refresh_token, "account_email": account_email}
    if provider == "box" and tok.get("access_token"):
        config["access_token"] = tok["access_token"]

    conn = Connection(
        tenant_id=meta.get("tenant_id"),
        name=meta["name"],
        source_type=provider,
        config_encrypted=encrypt_config(config),
        created_by=meta["user_id"],
        last_tested_at=datetime.utcnow(),
        last_test_status="ok",
        last_test_error=None,
    )
    session.add(conn)
    await session.commit()

    return _popup_html(True, f"Connected to {provider}" + (f" as {account_email}" if account_email else ""))


# ─── Popup HTML ─────────────────────────────────────────────────────


def _popup_html(ok: bool, message: str) -> HTMLResponse:
    color = "#10b981" if ok else "#ef4444"
    title = "Connected" if ok else "Failed"
    safe_msg = message.replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;background:#fafafa;
     display:flex;align-items:center;justify-content:center;height:100vh;margin:0;color:#222}}
.card{{background:#fff;padding:32px 40px;border-radius:12px;
      box-shadow:0 6px 24px rgba(0,0,0,.08);max-width:420px;text-align:center}}
.dot{{display:inline-block;width:14px;height:14px;border-radius:50%;
     background:{color};margin-right:10px;vertical-align:middle}}
h1{{margin:0 0 8px;font-size:20px}}
p{{color:#555;margin:6px 0 0}}
small{{color:#888;display:block;margin-top:14px}}
</style></head><body>
<div class="card">
  <h1><span class="dot"></span>{title}</h1>
  <p>{safe_msg}</p>
  <small>This window will close automatically…</small>
</div>
<script>
  try {{ window.opener && window.opener.postMessage(
    {{type:"retrace-oauth", ok:{str(ok).lower()}, message:{json.dumps(message)}}}, "*"); }} catch(e) {{}}
  setTimeout(function() {{ window.close(); }}, 1200);
</script>
</body></html>"""
    return HTMLResponse(content=body)
