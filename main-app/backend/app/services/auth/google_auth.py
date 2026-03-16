"""
Google Workspace OAuth 2.0 authentication.

Tenant must have google_config set:
{
  "client_id": "your-client-id.apps.googleusercontent.com",
  "client_secret": "your-client-secret",
  "redirect_uri": "http://localhost:5173/auth/callback/google",
  "allowed_domain": "company.com"   (optional — restrict to specific domain)
}
"""

from typing import Optional
import httpx
import structlog

logger = structlog.get_logger()

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


class GoogleUserInfo:
    def __init__(self, email: str, display_name: str, google_id: str, picture: str = ""):
        self.email = email
        self.display_name = display_name
        self.google_id = google_id
        self.picture = picture


async def exchange_code_for_user(
    code: str,
    google_config: dict,
) -> Optional[GoogleUserInfo]:
    """Exchange a Google authorization code for user info."""
    client_id = google_config.get("client_id", "")
    client_secret = google_config.get("client_secret", "")
    redirect_uri = google_config.get("redirect_uri", "")
    allowed_domain = google_config.get("allowed_domain", "")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            logger.error("google_token_exchange_failed", status=token_resp.status_code)
            return None

        access_token = token_resp.json().get("access_token")
        if not access_token:
            return None

        me_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if me_resp.status_code != 200:
            logger.error("google_userinfo_failed", status=me_resp.status_code)
            return None

        me = me_resp.json()
        email = me.get("email", "").lower()
        display_name = me.get("name", email.split("@")[0])
        google_id = me.get("id", "")
        picture = me.get("picture", "")

        if allowed_domain and not email.endswith(f"@{allowed_domain}"):
            logger.warning("google_domain_mismatch", email=email, allowed=allowed_domain)
            return None

        logger.info("google_auth_success", email=email)
        return GoogleUserInfo(email=email, display_name=display_name, google_id=google_id, picture=picture)


def get_authorize_url(google_config: dict) -> str:
    client_id = google_config.get("client_id", "")
    redirect_uri = google_config.get("redirect_uri", "")
    return (
        f"https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={client_id}"
        f"&response_type=code"
        f"&redirect_uri={redirect_uri}"
        f"&scope=openid+email+profile"
        f"&access_type=offline"
        f"&prompt=consent"
    )
