"""
Provider factory — returns the right adapter for a given connection.
"""

from app.models.email_connection import EmailConnection
from app.services.email.base import EmailProviderBase
from app.services.email.microsoft import MicrosoftProvider
from app.services.email.google import GoogleProvider
from app.services.email.zoho import ZohoProvider


def get_provider(
    connection: EmailConnection,
    client_id: str = "",
    client_secret: str = "",
    tenant_id: str = "common",
) -> EmailProviderBase:
    """Instantiate the correct provider adapter for *connection.provider*."""
    match connection.provider:
        case "microsoft":
            return MicrosoftProvider(
                client_id=client_id,
                client_secret=client_secret,
                tenant_id=tenant_id,
            )
        case "google":
            return GoogleProvider(
                client_id=client_id,
                client_secret=client_secret,
            )
        case "zoho":
            return ZohoProvider(
                client_id=client_id,
                client_secret=client_secret,
            )
        case _:
            raise ValueError(f"Unsupported email provider: {connection.provider}")
