"""Configuration loaded from environment variables."""

from pydantic_settings import BaseSettings


class ServiceNowConfig(BaseSettings):
    """All values come from env vars (prefixed SERVICENOW_)."""

    instance_url: str = ""  # e.g. https://dev12345.service-now.com
    auth_type: str = "basic"  # basic | oauth
    username: str = ""
    password: str = ""
    oauth_token: str = ""  # Azure AD / OAuth2 bearer token for pass-through

    model_config = {"env_prefix": "SERVICENOW_"}


config = ServiceNowConfig()
