"""
DEPRECATED: /api/v1/products endpoints.

This module is a thin alias over app.api.teams. Routes are re-exposed under the
legacy `/products` mount with Deprecation/Link response headers so clients can
discover the successor resource. Response bodies now use the new
`team_id`/`team_name` field names — callers must migrate accordingly. Full
schema aliasing is a follow-up task.
"""

import warnings

from fastapi import APIRouter, Depends
from starlette.responses import Response

from app.api.teams import router as teams_router

# Emit a one-time deprecation warning when this shim is imported.
warnings.warn(
    "app.api.products is deprecated; use app.api.teams. "
    "/api/v1/products will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)


async def _deprecation_headers(response: Response) -> None:
    """Add Deprecation + Link headers advertising the successor resource."""
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</api/v1/teams>; rel="successor-version"'


# Re-expose all team routes under this router. Clients hitting
# /api/v1/products/... reach the same team handlers, with deprecation headers
# attached to every response.
router = APIRouter(dependencies=[Depends(_deprecation_headers)])
# Copy route objects directly (include_router rejects empty-prefix+empty-path
# combinations when the child router has a route registered at "").
for _route in teams_router.routes:
    router.routes.append(_route)
