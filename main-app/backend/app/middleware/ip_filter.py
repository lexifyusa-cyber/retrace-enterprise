"""IP allow/deny list middleware."""

import ipaddress
from typing import Optional

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from sqlalchemy import select

from app.db.database import async_session_maker
from app.models.tenant import Tenant


def _ip_matches(client_ip: str, patterns: list) -> bool:
    """Check if client_ip matches any pattern in the list (supports CIDR)."""
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for pattern in patterns:
        try:
            if "/" in str(pattern):
                if addr in ipaddress.ip_network(str(pattern), strict=False):
                    return True
            elif str(addr) == str(pattern).strip():
                return True
        except ValueError:
            continue
    return False


class IPFilterMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/v1/auth/login") or request.url.path == "/" or request.url.path.startswith("/docs") or request.url.path.startswith("/openapi"):
            return await call_next(request)
        
        client_ip = request.client.host if request.client else None
        if not client_ip:
            return await call_next(request)

        try:
            async with async_session_maker() as session:
                result = await session.execute(select(Tenant).limit(1))
                tenant = result.scalar_one_or_none()
                if not tenant:
                    return await call_next(request)

                denylist = tenant.ip_denylist or []
                if isinstance(denylist, list) and denylist and _ip_matches(client_ip, denylist):
                    return JSONResponse(status_code=403, content={"detail": "Access denied: IP address blocked"})

                allowlist = tenant.ip_allowlist or []
                if isinstance(allowlist, list) and allowlist and not _ip_matches(client_ip, allowlist):
                    return JSONResponse(status_code=403, content={"detail": "Access denied: IP address not in allowlist"})
        except Exception:
            pass

        return await call_next(request)
