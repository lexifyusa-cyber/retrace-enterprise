"""
Base interface for all gateway providers.
Each provider maps Retrace consumer IDs to vendor-specific keys.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class GatewayCredentials:
    api_key: str
    base_url: Optional[str]


@dataclass
class ConsumerInfo:
    consumer_id: str
    vendor_consumer_id: Optional[str]
    vendor_api_key: str
    rate_limit_rpm: int = 60


class GatewayProvider(ABC):
    """Vendor-agnostic gateway interface."""

    @abstractmethod
    async def get_credentials(self, consumer_id: Optional[str] = None) -> GatewayCredentials:
        """Return api_key and base_url to use for LLM calls."""
        ...

    @abstractmethod
    async def create_consumer(
        self,
        consumer_id: str,
        metadata: Optional[dict] = None,
    ) -> ConsumerInfo:
        """Create a new consumer in the gateway vendor. Returns vendor key info."""
        ...

    @abstractmethod
    async def delete_consumer(self, consumer_id: str) -> None:
        """Remove a consumer from the gateway vendor."""
        ...

    @abstractmethod
    async def get_usage(self, consumer_id: str) -> dict:
        """Fetch usage stats for a consumer from the gateway vendor."""
        ...
