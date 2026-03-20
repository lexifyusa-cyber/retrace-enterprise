"""
Zuplo gateway provider — manages consumers and keys via the Zuplo API.
"""

from typing import Optional

import httpx
import structlog

from app.services.gateway.provider import (
    GatewayProvider,
    GatewayCredentials,
    ConsumerInfo,
)

logger = structlog.get_logger()

_ZUPLO_API_BASE = "https://dev.zuplo.com"


class ZuploProvider(GatewayProvider):
    """Zuplo API Key management and proxying."""

    def __init__(self, base_url: str, admin_key: str, account_name: str = "", project_name: str = "", env_name: str = "main"):
        self._base_url = base_url.rstrip("/")
        self._admin_key = admin_key
        self._account_name = account_name
        self._project_name = project_name
        self._env_name = env_name

    def _admin_url(self, path: str) -> str:
        return f"{_ZUPLO_API_BASE}/v1/accounts/{self._account_name}/key-buckets/{self._project_name}_{self._env_name}{path}"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._admin_key}",
            "Content-Type": "application/json",
        }

    async def get_credentials(self, consumer_id: Optional[str] = None) -> GatewayCredentials:
        return GatewayCredentials(api_key="", base_url=self._base_url)

    async def create_consumer(self, consumer_id: str, metadata: Optional[dict] = None) -> ConsumerInfo:
        payload = {
            "name": consumer_id,
            "description": f"ReTrace consumer {consumer_id}",
            "metadata": metadata or {},
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                self._admin_url("/consumers"),
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return ConsumerInfo(
                    consumer_id=consumer_id,
                    vendor_consumer_id=data.get("id", ""),
                    vendor_api_key=data.get("key", ""),
                    rate_limit_rpm=60,
                )
            logger.error("zuplo_create_consumer_failed", status=resp.status_code, body=resp.text[:200])
            raise RuntimeError(f"Zuplo consumer creation failed: {resp.status_code}")

    async def delete_consumer(self, consumer_id: str) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(
                self._admin_url(f"/consumers/{consumer_id}"),
                headers=self._headers(),
            )
            if resp.status_code not in (200, 204, 404):
                logger.warning("zuplo_delete_consumer_failed", status=resp.status_code)

    async def get_usage(self, consumer_id: str) -> dict:
        return {"provider": "zuplo", "consumer": consumer_id, "note": "Check Zuplo analytics dashboard for detailed usage"}
