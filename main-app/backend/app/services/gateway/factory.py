"""
Gateway provider factory — returns the correct provider based on GATEWAY_PROVIDER config.
"""

from typing import Optional

from app.core.config import settings
from app.services.gateway.provider import GatewayProvider
from app.services.gateway.direct import DirectProvider


def get_gateway_provider(
    api_key: str = "",
    api_url: Optional[str] = None,
) -> GatewayProvider:
    """
    Return the configured gateway provider.

    For "direct" mode, api_key and api_url come from DB settings (current behaviour).
    For vendor modes, GATEWAY_BASE_URL and GATEWAY_ADMIN_KEY come from env.
    """
    provider_name = (settings.GATEWAY_PROVIDER or "direct").lower().strip()

    if provider_name == "zuplo":
        from app.services.gateway.zuplo import ZuploProvider
        return ZuploProvider(
            base_url=settings.GATEWAY_BASE_URL,
            admin_key=settings.GATEWAY_ADMIN_KEY,
        )

    return DirectProvider(api_key=api_key, api_url=api_url)
