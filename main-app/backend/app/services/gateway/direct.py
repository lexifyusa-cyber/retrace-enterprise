"""
Direct provider — no gateway; LLM calls go straight to OpenAI/Anthropic.
This is the current default behaviour and requires zero external setup.
"""

from typing import Optional

from app.services.gateway.provider import (
    GatewayProvider,
    GatewayCredentials,
    ConsumerInfo,
)


class DirectProvider(GatewayProvider):
    """Bypass gateway; use the api_key and api_url from DB settings directly."""

    def __init__(self, api_key: str, api_url: Optional[str] = None):
        self._api_key = api_key
        self._api_url = api_url

    async def get_credentials(self, consumer_id: Optional[str] = None) -> GatewayCredentials:
        return GatewayCredentials(api_key=self._api_key, base_url=self._api_url)

    async def create_consumer(self, consumer_id: str, metadata: Optional[dict] = None) -> ConsumerInfo:
        return ConsumerInfo(
            consumer_id=consumer_id,
            vendor_consumer_id=None,
            vendor_api_key=self._api_key,
        )

    async def delete_consumer(self, consumer_id: str) -> None:
        pass

    async def get_usage(self, consumer_id: str) -> dict:
        return {"provider": "direct", "message": "No gateway — usage not tracked externally"}
