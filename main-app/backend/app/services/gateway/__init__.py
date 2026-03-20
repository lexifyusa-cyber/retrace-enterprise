"""
API Gateway abstraction layer — vendor-agnostic LLM proxy management.

Swap between Zuplo, Kong, AWS API Gateway, or direct-to-OpenAI
by changing a single environment variable (GATEWAY_PROVIDER).
"""

from app.services.gateway.provider import GatewayProvider
from app.services.gateway.factory import get_gateway_provider

__all__ = ["GatewayProvider", "get_gateway_provider"]
