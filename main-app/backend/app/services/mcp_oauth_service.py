"""
MCP OAuth 2.0 PKCE Service — handles OAuth discovery, client registration,
code exchange, and in-memory pending flow storage for HTTP MCP extensions.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx
import structlog

logger = structlog.get_logger()

# ── In-memory pending OAuth flows ─────────────────────────────────
_pending_flows: dict[str, dict] = {}


def generate_pkce() -> tuple[str, str]:
    """Generate a PKCE code_verifier and code_challenge (S256).

    Returns (code_verifier, code_challenge).
    """
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


async def discover_oauth_server(mcp_url: str) -> Optional[dict]:
    """Discover OAuth server metadata for an MCP URL.

    Implements the full MCP OAuth discovery chain:
      1. Probe the MCP URL — on 401, parse the WWW-Authenticate header for
         resource_metadata= to find the protected-resource document URL.
      2. Fetch the protected-resource document to get authorization_servers
         and scopes_supported.
      3. Fetch the authorization server's /.well-known/oauth-authorization-server.
      4. Fallback: try {origin}/.well-known/oauth-authorization-server directly.

    Returns the auth-server metadata dict (with scopes_supported merged in from
    the resource document when available), or None if discovery fails.
    """
    import re

    parsed = urlparse(mcp_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        # Step 1: probe the MCP endpoint to trigger 401 + WWW-Authenticate
        resource_metadata_url: Optional[str] = None
        resource_scopes: list[str] = []
        try:
            probe = await client.get(mcp_url, headers={"Accept": "application/json, text/event-stream"})
            if probe.status_code in (401, 403):
                www_auth = probe.headers.get("www-authenticate", "")
                # Extract resource_metadata="..." from the header value
                m = re.search(r'resource_metadata=["\']?([^\s,"\']+)["\']?', www_auth)
                if m:
                    resource_metadata_url = m.group(1).strip('"\'')
        except Exception as e:
            logger.debug("oauth_probe_failed", url=mcp_url, error=str(e))

        # Step 2: fetch protected-resource document
        auth_server_url: Optional[str] = None
        if resource_metadata_url:
            try:
                rm_resp = await client.get(resource_metadata_url)
                if rm_resp.status_code == 200:
                    rm_data = rm_resp.json()
                    servers = rm_data.get("authorization_servers", [])
                    if servers:
                        auth_server_url = servers[0]
                    resource_scopes = rm_data.get("scopes_supported", []) or []
                    logger.info("oauth_resource_metadata_fetched",
                                resource_metadata_url=resource_metadata_url,
                                auth_server=auth_server_url)
            except Exception as e:
                logger.debug("oauth_resource_metadata_failed", url=resource_metadata_url, error=str(e))

        # Step 3 + fallback: fetch auth-server metadata
        discovery_candidates: list[str] = []
        if auth_server_url:
            discovery_candidates.append(f"{auth_server_url.rstrip('/')}/.well-known/oauth-authorization-server")
        discovery_candidates.append(f"{origin}/.well-known/oauth-authorization-server")
        discovery_candidates.append(f"{mcp_url.rstrip('/')}/.well-known/oauth-authorization-server")

        for discovery_url in discovery_candidates:
            try:
                resp = await client.get(discovery_url)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("authorization_endpoint"):
                        # Merge scopes from resource document if auth server doesn't list them
                        if resource_scopes and not data.get("scopes_supported"):
                            data["scopes_supported"] = resource_scopes
                        logger.info("oauth_discovery_success", url=discovery_url)
                        return data
            except Exception as e:
                logger.debug("oauth_discovery_attempt_failed", url=discovery_url, error=str(e))

    logger.warning("oauth_discovery_failed", mcp_url=mcp_url)
    return None


async def register_oauth_client(registration_endpoint: str, redirect_uri: str) -> Optional[dict]:
    """Dynamically register an OAuth client via RFC 7591.

    POSTs to registration_endpoint with ReTrace client metadata.
    Returns the registration response JSON on success (200 or 201), or None.
    """
    payload = {
        "client_name": "ReTrace",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(registration_endpoint, json=payload)
            if resp.status_code in (200, 201):
                data = resp.json()
                logger.info("oauth_client_registered", client_id=data.get("client_id"))
                return data
            else:
                logger.warning("oauth_client_registration_failed",
                               status=resp.status_code, body=resp.text[:300])
        except Exception as e:
            logger.error("oauth_client_registration_error", error=str(e))

    return None


async def exchange_code_for_token(
    token_endpoint: str,
    code: str,
    code_verifier: str,
    client_id: str,
    redirect_uri: str,
    client_secret: Optional[str] = None,
) -> Optional[dict]:
    """Exchange an authorization code for tokens using PKCE.

    POSTs to token_endpoint with application/x-www-form-urlencoded.
    Returns the token response JSON on success, or None.
    """
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                token_endpoint,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code in (200, 201):
                token_data = resp.json()
                logger.info("oauth_token_exchanged", token_type=token_data.get("token_type"))
                return token_data
            else:
                logger.warning("oauth_token_exchange_failed",
                               status=resp.status_code, body=resp.text[:300])
        except Exception as e:
            logger.error("oauth_token_exchange_error", error=str(e))

    return None


def store_pending_flow(state: str, flow_data: dict) -> None:
    """Store a pending OAuth flow keyed by state, with a 10-minute TTL.

    Also cleans up any expired flows.
    """
    now = time.time()

    # Clean up expired flows
    expired_keys = [k for k, v in _pending_flows.items() if v.get("expires_at", 0) < now]
    for k in expired_keys:
        _pending_flows.pop(k, None)

    _pending_flows[state] = {**flow_data, "expires_at": now + 600}
    logger.debug("oauth_flow_stored", state=state[:8] + "...")


async def detect_oauth_required(mcp_url: str) -> bool:
    """Probe the MCP URL to check if OAuth authentication is required.

    Returns True if the server responds with 401/403 (OAuth challenge).
    """
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        try:
            resp = await client.get(mcp_url, headers={"Accept": "application/json"})
            if resp.status_code in (401, 403):
                return True
        except Exception:
            pass
    return False


def build_auth_url(
    auth_endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scopes: list[str] | None = None,
) -> str:
    """Build the OAuth authorization URL with PKCE parameters."""
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    return f"{auth_endpoint}?{urlencode(params)}"


def get_pending_flow(state: str) -> Optional[dict]:
    """Pop and return a pending flow by state.

    Returns None if the state is unknown or the flow has expired.
    """
    flow = _pending_flows.pop(state, None)
    if flow is None:
        return None

    if flow.get("expires_at", 0) < time.time():
        logger.warning("oauth_flow_expired", state=state[:8] + "...")
        return None

    return flow
