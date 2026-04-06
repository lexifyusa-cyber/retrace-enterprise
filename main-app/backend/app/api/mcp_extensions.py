"""
MCP Extensions API — CRUD and management for connected MCP servers.

Admin-only endpoints for configuring external MCP servers that the
ReTrace agent can use (like Claude Desktop's MCP server settings).
"""

import secrets
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CurrentUser, get_current_user, require_role
from app.db.database import get_session
from app.models.mcp_extension import McpExtension
from app.models.user_extension import UserExtension
from app.services.mcp_client_service import mcp_client_manager

logger = structlog.get_logger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class CreateExtensionRequest(BaseModel):
    name: str
    description: Optional[str] = None
    transport: str = "stdio"          # "stdio" or "http"
    url: Optional[str] = None         # required when transport="http"
    command: Optional[str] = None     # required when transport="stdio"
    args_json: Optional[list[str]] = None
    env_vars_json: Optional[dict[str, str]] = None
    auth_type: str = "none"
    auth_config_json: Optional[dict] = None
    is_enabled: bool = True


class UpdateExtensionRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    command: Optional[str] = None
    args_json: Optional[list[str]] = None
    env_vars_json: Optional[dict[str, str]] = None
    auth_type: Optional[str] = None
    auth_config_json: Optional[dict] = None
    is_enabled: Optional[bool] = None
    auto_install: Optional[bool] = None


class InstallExtensionRequest(BaseModel):
    env_vars_json: Optional[dict[str, str]] = None
    auth_config_json: Optional[dict] = None


class UpdateUserExtensionRequest(BaseModel):
    is_enabled: Optional[bool] = None
    env_vars_json: Optional[dict[str, str]] = None
    auth_config_json: Optional[dict] = None


# ---------------------------------------------------------------------------
# Endpoints — User Extension Management (per-user installs)
# IMPORTANT: These must be BEFORE /{extension_id} routes to avoid path conflicts
# ---------------------------------------------------------------------------

@router.get("/catalog")
async def list_catalog(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List published extensions available for users to install."""
    await _ensure_auto_install_extensions_provisioned(
        session, current_user.user_id, current_user.tenant_id
    )
    stmt = select(McpExtension).where(McpExtension.is_enabled == True)
    if current_user.tenant_id:
        stmt = stmt.where(
            (McpExtension.tenant_id == current_user.tenant_id)
            | (McpExtension.tenant_id.is_(None))
        )
    result = await session.execute(stmt.order_by(McpExtension.created_at.desc()))
    extensions = result.scalars().all()

    # Check which ones the current user has installed
    installed_result = await session.execute(
        select(UserExtension.extension_id, UserExtension.user_extension_id)
        .where(UserExtension.user_id == current_user.user_id)
    )
    installed_map = {row[0]: row[1] for row in installed_result.all()}

    items = []
    for ext in extensions:
        d = ext.to_dict()
        d["installed"] = ext.extension_id in installed_map
        d["user_extension_id"] = installed_map.get(ext.extension_id)
        items.append(d)

    return {"extensions": items, "count": len(items)}


@router.get("/my-extensions")
async def list_my_extensions(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List the current user's installed extensions."""
    await _ensure_auto_install_extensions_provisioned(
        session, current_user.user_id, current_user.tenant_id
    )
    stmt = (
        select(UserExtension, McpExtension)
        .join(McpExtension, UserExtension.extension_id == McpExtension.extension_id)
        .where(UserExtension.user_id == current_user.user_id)
        .order_by(UserExtension.installed_at.desc())
    )
    result = await session.execute(stmt)
    rows = result.all()

    items = []
    for user_ext, catalog_ext in rows:
        d = user_ext.to_dict()
        d["catalog"] = catalog_ext.to_dict()
        d["catalog"]["is_running"] = mcp_client_manager.is_running(
            catalog_ext.extension_id, current_user.user_id
        )
        items.append(d)

    return {"extensions": items, "count": len(items)}


@router.post("/my-extensions/{user_extension_id}/test")
async def test_my_extension(
    user_extension_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Test a user's installed extension with their own env vars."""
    user_ext = await _get_user_extension(user_extension_id, current_user.user_id, session)
    catalog_ext = await _get_extension(user_ext.extension_id, session)

    merged_env = user_ext.get_merged_env_vars(catalog_ext.get_raw_env_vars())
    merged_auth = {**(catalog_ext.auth_config_json or {}), **(user_ext.auth_config_json or {})}
    result = await mcp_client_manager.test_connection(
        transport=getattr(catalog_ext, "transport", "stdio") or "stdio",
        url=catalog_ext.url,
        command=catalog_ext.command,
        args=catalog_ext.args_json,
        env_vars=merged_env,
        auth_config=merged_auth,
    )
    return result


@router.patch("/my-extensions/{user_extension_id}")
async def update_my_extension(
    user_extension_id: str,
    body: UpdateUserExtensionRequest,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Update the current user's extension config (env vars, enable/disable)."""
    user_ext = await _get_user_extension(user_extension_id, current_user.user_id, session)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "env_vars_json" and value is not None and user_ext.env_vars_json:
            merged = dict(user_ext.env_vars_json)
            merged.update(value)
            setattr(user_ext, field, merged)
        else:
            setattr(user_ext, field, value)

    user_ext.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(user_ext)

    # Stop running connection so it picks up new config
    if mcp_client_manager.is_running(user_ext.extension_id, current_user.user_id):
        await mcp_client_manager.stop_extension(user_ext.extension_id, current_user.user_id)

    return user_ext.to_dict()


@router.delete("/my-extensions/{user_extension_id}")
async def delete_extension(
    user_extension_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Delete an extension — removes user install, catalog entry, server registry, and code files."""
    user_ext = await _get_user_extension(user_extension_id, current_user.user_id, session)

    # Block deletion of built-in extensions
    catalog_ext = await _get_extension(user_ext.extension_id, session)
    if catalog_ext.is_builtin:
        raise HTTPException(400, "Cannot delete built-in extensions.")

    # Stop any running MCP connections
    if mcp_client_manager.is_running(user_ext.extension_id, current_user.user_id):
        await mcp_client_manager.stop_extension(user_ext.extension_id, current_user.user_id)

    files_deleted = False
    server_deleted = False

    # If this is a Custom Build (has mcp_server_id), delete the server + code files
    if catalog_ext.mcp_server_id:
        try:
            from app.models.mcp_server import McpServer
            srv_result = await session.execute(
                select(McpServer).where(McpServer.server_id == catalog_ext.mcp_server_id)
            )
            srv = srv_result.scalar_one_or_none()
            if srv:
                # Delete code files from disk
                if srv.destination_folder:
                    import shutil
                    from pathlib import Path
                    folder = srv.destination_folder
                    if folder.startswith("~/") or folder.startswith("~\\"):
                        folder = str(Path.home() / folder[2:])
                    folder_path = Path(folder).resolve()

                    is_safe = (folder_path.parent.name == "mcp-servers"
                               and len(folder_path.parts) >= 3
                               and folder_path.name != "mcp-servers")
                    if is_safe and folder_path.is_dir():
                        shutil.rmtree(folder_path, ignore_errors=True)
                        files_deleted = True

                await session.delete(srv)
                server_deleted = True
        except Exception as srv_err:
            logger.warning("extension_delete_server_failed", error=str(srv_err))

    # Delete all user installs for this extension (not just current user)
    all_installs = await session.execute(
        select(UserExtension).where(UserExtension.extension_id == catalog_ext.extension_id)
    )
    for ue in all_installs.scalars().all():
        await session.delete(ue)

    # Delete the catalog entry
    await session.delete(catalog_ext)
    await session.commit()

    logger.info("extension_fully_deleted",
                extension_id=catalog_ext.extension_id,
                name=catalog_ext.name,
                server_deleted=server_deleted,
                files_deleted=files_deleted)
    return {
        "status": "deleted",
        "extension_id": catalog_ext.extension_id,
        "server_deleted": server_deleted,
        "files_deleted": files_deleted,
    }


@router.post("/my-extensions/{user_extension_id}/oauth/initiate")
async def initiate_oauth_flow(
    user_extension_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Initiate OAuth 2.0 PKCE flow for an HTTP MCP extension.

    Discovers the OAuth server, optionally registers a client dynamically,
    and returns the authorization URL to redirect the user to.
    """
    from app.services.mcp_oauth_service import (
        discover_oauth_server,
        generate_pkce,
        register_oauth_client,
        store_pending_flow,
    )

    user_ext = await _get_user_extension(user_extension_id, current_user.user_id, session)
    catalog_ext = await _get_extension(user_ext.extension_id, session)

    transport = getattr(catalog_ext, "transport", "stdio") or "stdio"
    if transport != "http" or not catalog_ext.url:
        raise HTTPException(400, "OAuth is only supported for HTTP transport extensions.")

    server_metadata = await discover_oauth_server(catalog_ext.url)
    if server_metadata is None:
        raise HTTPException(
            400,
            "Could not discover OAuth server for this MCP. It may not support the standard OAuth discovery protocol.",
        )

    authorization_endpoint = server_metadata.get("authorization_endpoint")
    token_endpoint = server_metadata.get("token_endpoint")
    registration_endpoint = server_metadata.get("registration_endpoint")

    if not authorization_endpoint or not token_endpoint:
        raise HTTPException(400, "OAuth server metadata is missing authorization_endpoint or token_endpoint.")

    redirect_uri = str(request.base_url).rstrip("/") + "/api/v1/mcp-extensions/oauth/callback"

    client_id: str | None = None
    client_secret: str | None = None

    if registration_endpoint:
        reg_result = await register_oauth_client(registration_endpoint, redirect_uri)
        if reg_result is None:
            raise HTTPException(500, "Failed to register OAuth client with the MCP server.")
        client_id = reg_result.get("client_id")
        client_secret = reg_result.get("client_secret")

    if not client_id:
        raise HTTPException(500, "OAuth client registration did not return a client_id.")

    code_verifier, code_challenge = generate_pkce()
    state = secrets.token_urlsafe(32)

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    scopes_supported = server_metadata.get("scopes_supported")
    if scopes_supported:
        params["scope"] = " ".join(scopes_supported) if isinstance(scopes_supported, list) else scopes_supported

    auth_url = f"{authorization_endpoint}?{urlencode(params)}"

    store_pending_flow(state, {
        "user_extension_id": user_extension_id,
        "user_id": current_user.user_id,
        "extension_id": catalog_ext.extension_id,
        "token_endpoint": token_endpoint,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    })

    logger.info("oauth_flow_initiated", user_extension_id=user_extension_id, user_id=current_user.user_id)
    return {"auth_url": auth_url, "state": state}


@router.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """OAuth 2.0 callback — exchanges the authorization code for tokens
    and stores them in the user's extension auth_config_json.
    """
    from app.services.mcp_oauth_service import exchange_code_for_token, get_pending_flow

    if error:
        return HTMLResponse(
            f"""<html><head><title>Authorization Failed</title></head><body>
<p style="font-family:sans-serif;text-align:center;margin-top:80px;color:#f87171">
  <strong>Authorization failed:</strong> {error}<br>You can close this window.
</p>
</body></html>""",
            status_code=400,
        )

    if not state:
        return HTMLResponse(
            """<html><head><title>Authorization Failed</title></head><body>
<p style="font-family:sans-serif;text-align:center;margin-top:80px;color:#f87171">
  <strong>Missing state parameter.</strong><br>You can close this window.
</p>
</body></html>""",
            status_code=400,
        )

    flow = get_pending_flow(state)
    if flow is None:
        return HTMLResponse(
            """<html><head><title>Authorization Failed</title></head><body>
<p style="font-family:sans-serif;text-align:center;margin-top:80px;color:#f87171">
  <strong>Invalid or expired authorization session.</strong><br>Please try connecting again.
</p>
</body></html>""",
            status_code=400,
        )

    token_data = await exchange_code_for_token(
        token_endpoint=flow["token_endpoint"],
        code=code,
        code_verifier=flow["code_verifier"],
        client_id=flow["client_id"],
        redirect_uri=flow["redirect_uri"],
        client_secret=flow.get("client_secret"),
    )

    if token_data is None:
        return HTMLResponse(
            """<html><head><title>Authorization Failed</title></head><body>
<p style="font-family:sans-serif;text-align:center;margin-top:80px;color:#f87171">
  <strong>Failed to exchange authorization code for tokens.</strong><br>You can close this window.
</p>
</body></html>""",
            status_code=500,
        )

    # Load the UserExtension and store the tokens
    result = await session.execute(
        select(UserExtension)
        .where(UserExtension.user_extension_id == flow["user_extension_id"])
        .where(UserExtension.user_id == flow["user_id"])
    )
    user_ext = result.scalar_one_or_none()
    if not user_ext:
        return HTMLResponse(
            """<html><head><title>Authorization Failed</title></head><body>
<p style="font-family:sans-serif;text-align:center;margin-top:80px;color:#f87171">
  <strong>Extension not found.</strong><br>You can close this window.
</p>
</body></html>""",
            status_code=400,
        )

    existing_auth = dict(user_ext.auth_config_json or {})
    existing_auth.update({
        "oauth_access_token": token_data.get("access_token"),
        "oauth_refresh_token": token_data.get("refresh_token"),
        "oauth_token_type": token_data.get("token_type", "Bearer"),
        "oauth_expires_in": token_data.get("expires_in"),
        "oauth_granted_at": int(time.time()),
    })
    user_ext.auth_config_json = existing_auth
    user_ext.updated_at = datetime.utcnow()
    await session.commit()

    user_extension_id = flow["user_extension_id"]
    logger.info("oauth_token_stored", user_extension_id=user_extension_id, user_id=flow["user_id"])

    return HTMLResponse(
        f"""<html><head><title>Authorization Successful</title></head><body>
<p style="font-family:sans-serif;text-align:center;margin-top:80px">
  <strong>Authorization successful!</strong><br>You can close this window.
</p>
<script>
  try {{
    window.opener && window.opener.postMessage(
      {{ type: 'mcp_oauth_success', user_extension_id: '{user_extension_id}' }}, '*'
    );
  }} catch(e) {{}}
  setTimeout(() => window.close(), 1500);
</script>
</body></html>"""
    )


@router.post("/{extension_id}/install")
async def install_extension(
    extension_id: str,
    body: InstallExtensionRequest,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Install a catalog extension to the current user's account."""
    ext = await _get_extension(extension_id, session)
    if not ext.is_enabled:
        raise HTTPException(400, "This extension is not published.")

    # Check if already installed
    existing = await session.execute(
        select(UserExtension)
        .where(UserExtension.extension_id == extension_id)
        .where(UserExtension.user_id == current_user.user_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Extension already installed.")

    user_ext = UserExtension(
        extension_id=extension_id,
        user_id=current_user.user_id,
        tenant_id=current_user.tenant_id,
        env_vars_json=body.env_vars_json,
        auth_config_json=body.auth_config_json,
    )
    session.add(user_ext)
    await session.commit()
    await session.refresh(user_ext)
    logger.info("user_extension_installed", extension_id=extension_id, user_id=current_user.user_id)
    return user_ext.to_dict()


# ---------------------------------------------------------------------------
# Endpoints — Admin Catalog CRUD (existing)
# ---------------------------------------------------------------------------

@router.get("")
async def list_extensions(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List all configured MCP extensions."""
    stmt = select(McpExtension)
    if current_user.tenant_id:
        stmt = stmt.where(
            (McpExtension.tenant_id == current_user.tenant_id)
            | (McpExtension.tenant_id.is_(None))
        )
    result = await session.execute(stmt.order_by(McpExtension.created_at.desc()))
    extensions = result.scalars().all()

    items = []
    for ext in extensions:
        d = ext.to_dict()
        d["is_running"] = mcp_client_manager.is_running(ext.extension_id)
        items.append(d)

    return {"extensions": items, "count": len(items)}


@router.post("")
async def create_extension(
    body: CreateExtensionRequest,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Create a new MCP extension configuration."""
    # Validate transport-specific required fields
    if body.transport == "http":
        if not body.url:
            raise HTTPException(400, "url is required for HTTP transport")
    else:
        if not body.command:
            raise HTTPException(400, "command is required for stdio transport")

    # Duplicate check: reject if an extension with the same command/url already exists
    if body.transport == "http":
        dup_result = await session.execute(
            select(McpExtension).where(McpExtension.url == body.url)
        )
    else:
        dup_result = await session.execute(
            select(McpExtension).where(McpExtension.command == body.command)
        )
    if dup_result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="An extension with this command already exists. MCP Builder servers are added automatically — check your installed extensions.",
        )

    ext = McpExtension(
        tenant_id=current_user.tenant_id,
        name=body.name,
        description=body.description,
        transport=body.transport,
        url=body.url,
        command=body.command,
        args_json=body.args_json,
        env_vars_json=body.env_vars_json,
        auth_type=body.auth_type,
        auth_config_json=body.auth_config_json,
        is_enabled=body.is_enabled,
        created_by=current_user.user_id,
    )
    session.add(ext)
    await session.flush()  # Get extension_id before commit

    # Auto-install for the creator
    user_ext = UserExtension(
        extension_id=ext.extension_id,
        user_id=current_user.user_id,
        tenant_id=current_user.tenant_id,
        is_enabled=True,
    )
    session.add(user_ext)

    # Auto-install for all users in the same tenant
    if current_user.tenant_id:
        from app.models.user import User
        tenant_users_result = await session.execute(
            select(User.user_id).where(
                User.tenant_id == current_user.tenant_id,
                User.user_id != current_user.user_id,
            )
        )
        for (uid,) in tenant_users_result.all():
            session.add(UserExtension(
                extension_id=ext.extension_id,
                user_id=uid,
                tenant_id=current_user.tenant_id,
                is_enabled=True,
            ))

    await session.commit()
    await session.refresh(ext)
    logger.info("external_extension_created", extension_id=ext.extension_id, name=ext.name, created_by=current_user.user_id)
    return ext.to_dict()


@router.get("/{extension_id}")
async def get_extension(
    extension_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get details for a single MCP extension."""
    ext = await _get_extension(extension_id, session)
    d = ext.to_dict()
    d["is_running"] = mcp_client_manager.is_running(ext.extension_id)
    return d


@router.patch("/{extension_id}")
async def update_extension(
    extension_id: str,
    body: UpdateExtensionRequest,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Update an MCP extension configuration."""
    ext = await _get_extension(extension_id, session)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        # For env_vars, merge rather than replace (allows partial updates)
        if field == "env_vars_json" and value is not None and ext.env_vars_json:
            merged = dict(ext.env_vars_json)
            merged.update(value)
            setattr(ext, field, merged)
        else:
            setattr(ext, field, value)

    ext.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(ext)

    # If the extension is running, stop it so it picks up new config on next use
    if mcp_client_manager.is_running(extension_id):
        await mcp_client_manager.stop_extension(extension_id)

    return ext.to_dict()


@router.delete("/{extension_id}")
async def delete_extension(
    extension_id: str,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Delete an MCP extension."""
    ext = await _get_extension(extension_id, session)

    # Block deletion of built-in extensions
    if ext.is_builtin:
        raise HTTPException(400, "Cannot delete built-in extensions.")

    # Stop if running
    if mcp_client_manager.is_running(extension_id):
        await mcp_client_manager.stop_extension(extension_id)

    await session.delete(ext)
    await session.commit()
    return {"status": "deleted", "extension_id": extension_id}


@router.post("/{extension_id}/test")
async def test_extension(
    extension_id: str,
    current_user: CurrentUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Test an MCP extension — spawn, discover tools, then shut down.

    Returns the list of discovered tools or an error message.
    """
    ext = await _get_extension(extension_id, session)
    result = await mcp_client_manager.test_connection(
        transport=getattr(ext, "transport", "stdio") or "stdio",
        url=ext.url,
        command=ext.command,
        args=ext.args_json,
        env_vars=ext.get_raw_env_vars(),
    )
    return result


@router.get("/{extension_id}/tools")
async def get_extension_tools(
    extension_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List tools from a running MCP extension.

    If the extension is not running, it will be started temporarily to discover tools.
    """
    ext = await _get_extension(extension_id, session)

    if mcp_client_manager.is_running(extension_id):
        tools = await mcp_client_manager.list_tools(extension_id)
    else:
        # Start temporarily to discover tools
        tools = await mcp_client_manager.start_extension(
            extension_id=ext.extension_id,
            extension_name=ext.name,
            transport=getattr(ext, "transport", "stdio") or "stdio",
            url=ext.url,
            command=ext.command,
            args=ext.args_json,
            env_vars=ext.get_raw_env_vars(),
            auth_type=ext.auth_type,
            auth_config=ext.auth_config_json,
        )

    return {
        "extension_id": extension_id,
        "tools": [{"name": t.name, "description": t.description} for t in tools],
        "count": len(tools),
    }


@router.post("/{extension_id}/validate-user-access")
async def validate_user_access(
    extension_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Check if the current user's Azure AD token grants access to the external service.

    For oauth_passthrough extensions (e.g. ServiceNow), verifies that a matching
    user account exists in the target system.
    """
    ext = await _get_extension(extension_id, session)

    if ext.auth_type not in ("oauth_passthrough", "ldap_passthrough", "auto_passthrough"):
        return {"has_access": True, "message": "This extension does not use credential pass-through."}

    # Fetch user's active session
    from app.models.user_session import UserSession
    sess_result = await session.execute(
        select(UserSession)
        .where(UserSession.user_id == current_user.user_id)
        .where(UserSession.revoked == False)
        .order_by(UserSession.last_active_at.desc())
        .limit(1)
    )
    user_session = sess_result.scalar_one_or_none()

    # ── Auto pass-through: detect which auth the user has and route accordingly ──
    effective_auth_type = ext.auth_type
    if ext.auth_type == "auto_passthrough":
        if user_session and user_session.external_username and user_session.external_password:
            effective_auth_type = "ldap_passthrough"
        elif user_session and (user_session.external_id_token or user_session.external_access_token):
            effective_auth_type = "oauth_passthrough"
        else:
            return {
                "has_access": False,
                "message": "No authentication credentials found. Please sign out and sign in again (via Azure AD or LDAP).",
            }

    # ── LDAP pass-through validation ──
    if effective_auth_type == "ldap_passthrough":
        if not user_session or not user_session.external_username or not user_session.external_password:
            return {"has_access": False, "message": "Sign in with LDAP to verify access. No LDAP credentials found in your session."}

        from app.core.encryption import decrypt_value
        username = user_session.external_username
        password = decrypt_value(user_session.external_password)

        env_vars = ext.env_vars_json or {}
        instance_url = env_vars.get("SERVICENOW_INSTANCE_URL", "").rstrip("/")
        if not instance_url:
            return {"has_access": False, "message": "No SERVICENOW_INSTANCE_URL configured on this extension."}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{instance_url}/api/now/table/sys_user",
                    params={
                        "sysparm_query": f"user_name={username}",
                        "sysparm_limit": "1",
                        "sysparm_fields": "sys_id,name,email,user_name",
                    },
                    auth=httpx.BasicAuth(username, password),
                )

            if resp.status_code == 401:
                return {"has_access": False, "message": "LDAP credentials were rejected by ServiceNow (401). Ensure a matching local account exists."}

            if resp.status_code != 200:
                return {"has_access": False, "message": f"ServiceNow returned HTTP {resp.status_code}."}

            data = resp.json()
            results = data.get("result", [])
            if results:
                sn_user = results[0]
                return {
                    "has_access": True,
                    "servicenow_user": {
                        "name": sn_user.get("name", ""),
                        "email": sn_user.get("email", ""),
                        "user_name": sn_user.get("user_name", ""),
                    },
                    "message": f"Connected as {sn_user.get('name', sn_user.get('user_name', 'unknown'))}",
                }
            else:
                return {"has_access": False, "message": f"No ServiceNow account found for user_name={username}. Ask your admin to create a matching account."}
        except httpx.TimeoutException:
            return {"has_access": False, "message": "ServiceNow request timed out. Check the instance URL."}
        except Exception as exc:
            from app.core.config import settings
            msg = f"Verification failed: {exc}" if settings.is_development else "Verification failed. Check server logs for details."
            logger.error("validate_user_access_ldap_error", error=str(exc))
            return {"has_access": False, "message": msg}

    # ── OAuth pass-through validation ──
    if not user_session or not (user_session.external_id_token or user_session.external_access_token):
        return {
            "has_access": False,
            "message": "Sign in with Azure AD to verify access. No active Azure AD session found.",
        }

    # Prefer ID token (aud=client_id, accepted by ServiceNow OIDC) over access token (aud=graph)
    bearer_token = user_session.external_id_token or user_session.external_access_token

    # Debug: decode JWT claims (without verification) to check aud, iss, email
    # Only in development mode to avoid leaking token details in production
    from app.core.config import settings
    token_type = "id_token" if user_session.external_id_token else "access_token"
    debug_claims = {}
    if settings.is_development:
        try:
            import base64, json as _json
            parts = bearer_token.split(".")
            # Decode payload (part 1) — add padding
            payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(payload_b64))
            debug_claims = {
                "token_type": token_type,
                "aud": claims.get("aud"),
                "iss": claims.get("iss"),
                "email": claims.get("email"),
                "upn": claims.get("upn"),
                "preferred_username": claims.get("preferred_username"),
                "sub": claims.get("sub", "")[:20] + "...",
                "exp": claims.get("exp"),
            }
            logger.info("verify_access_token_debug", **debug_claims)
        except Exception as e:
            logger.error("jwt_decode_debug_failed", error=str(e))

    # Get the instance URL from env vars
    env_vars = ext.env_vars_json or {}
    instance_url = env_vars.get("SERVICENOW_INSTANCE_URL", "").rstrip("/")
    if not instance_url:
        return {"has_access": False, "message": "No SERVICENOW_INSTANCE_URL configured on this extension."}

    # Query ServiceNow for a user matching the current user's email
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{instance_url}/api/now/table/sys_user",
                params={
                    "sysparm_query": f"email={current_user.email}",
                    "sysparm_limit": "1",
                    "sysparm_fields": "sys_id,name,email,user_name",
                },
                headers={"Authorization": f"Bearer {bearer_token}"},
            )

        if resp.status_code == 401:
            logger.error(
                "servicenow_token_rejected",
                status=resp.status_code,
                response_body=resp.text[:500],
                token_type=token_type,
            )
            result = {
                "has_access": False,
                "message": f"Azure AD {token_type} was rejected by ServiceNow (401).",
            }
            if settings.is_development:
                result["message"] += f" Response: {resp.text[:200]}"
                result["debug"] = debug_claims
            return result

        if resp.status_code != 200:
            return {"has_access": False, "message": f"ServiceNow returned HTTP {resp.status_code}."}

        data = resp.json()
        results = data.get("result", [])
        if results:
            sn_user = results[0]
            return {
                "has_access": True,
                "servicenow_user": {
                    "name": sn_user.get("name", ""),
                    "email": sn_user.get("email", ""),
                    "user_name": sn_user.get("user_name", ""),
                },
                "message": f"Connected as {sn_user.get('name', sn_user.get('email', 'unknown'))}",
            }
        else:
            return {
                "has_access": False,
                "message": f"No ServiceNow account found for {current_user.email}. Ask your admin to create a matching user.",
            }
    except httpx.TimeoutException:
        return {"has_access": False, "message": "ServiceNow request timed out. Check the instance URL."}
    except Exception as exc:
        logger.error("validate_user_access_error", error=str(exc))
        msg = f"Verification failed: {exc}" if settings.is_development else "Verification failed. Check server logs for details."
        return {"has_access": False, "message": msg}


async def _get_user_extension(
    user_extension_id: str, user_id: str, session: AsyncSession
) -> UserExtension:
    result = await session.execute(
        select(UserExtension)
        .where(UserExtension.user_extension_id == user_extension_id)
        .where(UserExtension.user_id == user_id)
    )
    user_ext = result.scalar_one_or_none()
    if not user_ext:
        raise HTTPException(status_code=404, detail="User extension not found")
    return user_ext


async def _get_extension(extension_id: str, session: AsyncSession) -> McpExtension:
    result = await session.execute(
        select(McpExtension).where(McpExtension.extension_id == extension_id)
    )
    ext = result.scalar_one_or_none()
    if not ext:
        raise HTTPException(status_code=404, detail="MCP extension not found")
    return ext


async def _ensure_auto_install_extensions_provisioned(
    session: AsyncSession,
    user_id: str,
    tenant_id: Optional[str],
) -> None:
    """Idempotent: create a UserExtension record for every auto_install=True
    extension this user does not yet have.  Admin-controlled per extension —
    only extensions explicitly flagged auto_install=True are pushed to all users."""
    stmt = select(McpExtension).where(
        McpExtension.auto_install == True,
        McpExtension.is_enabled == True,
    )
    if tenant_id:
        stmt = stmt.where(
            (McpExtension.tenant_id == tenant_id)
            | (McpExtension.tenant_id.is_(None))
        )
    else:
        stmt = stmt.where(McpExtension.tenant_id.is_(None))

    result = await session.execute(stmt)
    auto_exts = result.scalars().all()
    if not auto_exts:
        return

    existing_result = await session.execute(
        select(UserExtension.extension_id)
        .where(UserExtension.user_id == user_id)
        .where(UserExtension.extension_id.in_([e.extension_id for e in auto_exts]))
    )
    already_installed = {row[0] for row in existing_result.all()}

    for ext in auto_exts:
        if ext.extension_id not in already_installed:
            session.add(UserExtension(
                extension_id=ext.extension_id,
                user_id=user_id,
                tenant_id=tenant_id,
                is_enabled=True,
            ))
            logger.info("auto_install_extension_provisioned",
                        extension_id=ext.extension_id, user_id=user_id)

    await session.flush()
