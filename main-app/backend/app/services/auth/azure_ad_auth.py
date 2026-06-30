"""
Azure AD / Entra ID authentication via OAuth 2.0 Authorization Code flow.

Tenant must have azure_ad_config set:
{
  "client_id": "your-app-client-id",
  "client_secret": "your-app-client-secret",
  "tenant_id": "your-azure-tenant-id",   (or "common" for multi-tenant)
  "redirect_uri": "http://localhost:5173/auth/callback/azure"
}

Frontend flow:
1. Redirect user to: https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize
   ?client_id=...&response_type=code&redirect_uri=...&scope=openid profile email User.Read
2. User approves, redirected back with ?code=...
3. Frontend sends code to POST /api/v1/auth/azure-ad
4. Backend exchanges code for tokens, reads user info, creates/updates user
"""

from typing import Optional
import base64
import json
import httpx
import structlog

logger = structlog.get_logger()

AZURE_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
AZURE_GRAPH_ME = "https://graph.microsoft.com/v1.0/me"


class AzureADUserInfo:
    def __init__(
        self,
        email: str,
        display_name: str,
        azure_id: str,
        access_token: str = "",
        id_token: str = "",
        refresh_token: str = "",
        expires_in: int = 0,
        claims: dict | None = None,
    ):
        self.email = email
        self.display_name = display_name
        self.azure_id = azure_id
        # Azure AD tokens for pass-through to MCP extensions (ServiceNow, etc.)
        self.access_token = access_token
        self.id_token = id_token  # JWT with aud=client_id, used for ServiceNow REST API
        self.refresh_token = refresh_token
        self.expires_in = expires_in  # seconds until access_token expires
        self.claims = claims or {}


async def exchange_code_for_user(
    code: str,
    azure_config: dict,
) -> Optional[AzureADUserInfo]:
    """Exchange an Azure AD authorization code for user info."""
    client_id = azure_config.get("client_id", "")
    client_secret = azure_config.get("client_secret", "")
    tenant_id = azure_config.get("tenant_id", "common")
    redirect_uri = azure_config.get("redirect_uri", "")

    token_url = AZURE_TOKEN_URL.format(tenant_id=tenant_id)

    async with httpx.AsyncClient() as client:
        # Exchange code for tokens
        token_resp = await client.post(
            token_url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "scope": _build_scope(azure_config),
            },
        )
        if token_resp.status_code != 200:
            logger.error("azure_ad_token_exchange_failed", status=token_resp.status_code, body=token_resp.text[:500])
            return None

        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return None

        # Fetch user profile from Microsoft Graph
        me_resp = await client.get(
            AZURE_GRAPH_ME,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if me_resp.status_code != 200:
            logger.error("azure_ad_graph_me_failed", status=me_resp.status_code)
            return None

        me = me_resp.json()
        email = me.get("mail") or me.get("userPrincipalName", "")
        display_name = me.get("displayName", email.split("@")[0] if email else "")
        azure_id = me.get("id", "")

        id_claims = _decode_unverified_jwt(token_data.get("id_token", ""))
        access_claims = _decode_unverified_jwt(access_token)
        claims = {**access_claims, **id_claims}

        logger.info("azure_ad_auth_success", email=email, azure_id=azure_id)
        return AzureADUserInfo(
            email=email.lower(),
            display_name=display_name,
            azure_id=azure_id,
            access_token=access_token,
            id_token=token_data.get("id_token", ""),
            refresh_token=token_data.get("refresh_token", ""),
            expires_in=int(token_data.get("expires_in", 3600)),
            claims=claims,
        )


async def test_azure_ad_connection(azure_config: dict) -> dict:
    """Test Azure AD connectivity by attempting a client-credentials token request.
    Returns {"success": True/False, "message": "..."}.
    """
    client_id = azure_config.get("client_id", "")
    client_secret = azure_config.get("client_secret", "")
    tenant_id = azure_config.get("tenant_id", "common")

    if not client_id:
        return {"success": False, "message": "Application (Client) ID is required."}
    if not client_secret:
        return {"success": False, "message": "Client Secret is required."}

    token_url = AZURE_TOKEN_URL.format(tenant_id=tenant_id)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(token_url, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            })
        if resp.status_code == 200:
            logger.info("azure_ad_test_connection_success", client_id=client_id)
            return {"success": True, "message": "Azure AD credentials validated successfully."}

        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        error_desc = body.get("error_description", resp.text[:200])
        logger.error("azure_ad_test_connection_failed", status=resp.status_code, error=error_desc)
        return {"success": False, "message": f"Azure AD returned {resp.status_code}: {error_desc}"}
    except Exception as exc:
        logger.error("azure_ad_test_connection_exception", error=str(exc))
        return {"success": False, "message": f"Azure AD connection test failed: {exc}"}


async def refresh_azure_ad_token(refresh_token: str, azure_config: dict) -> dict:
    """Refresh an expired Azure AD access token using the refresh token.
    Returns {"access_token", "refresh_token", "expires_in"} on success,
    or {"error": "..."} on failure.
    """
    client_id = azure_config.get("client_id", "")
    client_secret = azure_config.get("client_secret", "")
    tenant_id = azure_config.get("tenant_id", "common")
    full_scope = _build_scope(azure_config)

    token_url = AZURE_TOKEN_URL.format(tenant_id=tenant_id)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(token_url, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": full_scope,
            })
        if resp.status_code == 200:
            data = resp.json()
            logger.info("azure_ad_token_refresh_success", client_id=client_id)
            return {
                "access_token": data.get("access_token", ""),
                "id_token": data.get("id_token", ""),
                "refresh_token": data.get("refresh_token", refresh_token),
                "expires_in": int(data.get("expires_in", 3600)),
            }
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        error_desc = body.get("error_description", resp.text[:200])
        logger.error("azure_ad_token_refresh_failed", status=resp.status_code, error=error_desc)
        return {"error": f"Token refresh failed: {error_desc}"}
    except Exception as exc:
        logger.error("azure_ad_token_refresh_exception", error=str(exc))
        return {"error": f"Token refresh failed: {exc}"}


DEFAULT_DELEGATED_SCOPES = (
    "openid profile email offline_access "
    "User.Read Mail.Read Calendars.Read Channel.ReadBasic.All "
    "ChannelMessage.Read.All Chat.Read"
)


def _dedupe_scope_string(scope: str) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in (scope or "").split():
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return " ".join(ordered)


def _build_scope(azure_config: dict) -> str:
    """Build scopes for ReTrace login and delegated MCP SSO.

    The defaults request offline refresh plus Microsoft Graph delegated scopes
    used by the built-in Teams and Outlook MCPs. Tenants can add app-specific
    scopes, for example api://.../servicenow.access. Azure admin consent may
    still be required in Entra ID for these permissions.
    """
    additional = azure_config.get("additional_scopes", "")
    return _dedupe_scope_string(f"{DEFAULT_DELEGATED_SCOPES} {additional}")


def _decode_unverified_jwt(token: str) -> dict:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
    except Exception:
        return {}


def resolve_retrace_role_from_claims(
    claims: dict,
    azure_config: dict,
    fallback_role: str = "user",
) -> str:
    """Map Entra app roles/groups to ReTrace RBAC roles.

    azure_ad_config.role_mappings accepts values like:
      {"admin": ["Retrace.Admin", "<group-object-id>"],
       "user_admin": ["Retrace.UserAdmin"],
       "user": ["Retrace.User"]}

    Values are matched against the token's roles, groups, wids, scp, and oid.
    zero_admin is intentionally not grantable from Entra.
    """
    mappings = azure_config.get("role_mappings") or {}
    if not isinstance(mappings, dict):
        return fallback_role

    claim_values: set[str] = set()
    for key in ("roles", "groups", "wids"):
        raw = claims.get(key)
        if isinstance(raw, list):
            claim_values.update(str(v) for v in raw if v)
        elif raw:
            claim_values.add(str(raw))
    if claims.get("scp"):
        claim_values.update(str(claims["scp"]).split())
    for key in ("oid", "appid", "azp"):
        if claims.get(key):
            claim_values.add(str(claims[key]))

    for role in ("admin", "user_admin", "user"):
        allowed = mappings.get(role) or []
        if isinstance(allowed, str):
            allowed = [allowed]
        if any(str(value) in claim_values for value in allowed):
            return role
    return fallback_role


def get_authorize_url(azure_config: dict) -> str:
    """Build the Azure AD authorization URL for the frontend redirect."""
    tenant_id = azure_config.get("tenant_id", "common")
    client_id = azure_config.get("client_id", "")
    redirect_uri = azure_config.get("redirect_uri", "")
    scope = _build_scope(azure_config).replace(" ", "+")
    return (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
        f"?client_id={client_id}"
        f"&response_type=code"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope}"
        f"&response_mode=query"
    )
