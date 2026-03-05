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
import httpx
import structlog

logger = structlog.get_logger()

AZURE_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
AZURE_GRAPH_ME = "https://graph.microsoft.com/v1.0/me"


class AzureADUserInfo:
    def __init__(self, email: str, display_name: str, azure_id: str):
        self.email = email
        self.display_name = display_name
        self.azure_id = azure_id


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
                "scope": "openid profile email User.Read",
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

        logger.info("azure_ad_auth_success", email=email, azure_id=azure_id)
        return AzureADUserInfo(email=email.lower(), display_name=display_name, azure_id=azure_id)


def get_authorize_url(azure_config: dict) -> str:
    """Build the Azure AD authorization URL for the frontend redirect."""
    tenant_id = azure_config.get("tenant_id", "common")
    client_id = azure_config.get("client_id", "")
    redirect_uri = azure_config.get("redirect_uri", "")
    return (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
        f"?client_id={client_id}"
        f"&response_type=code"
        f"&redirect_uri={redirect_uri}"
        f"&scope=openid+profile+email+User.Read"
        f"&response_mode=query"
    )
