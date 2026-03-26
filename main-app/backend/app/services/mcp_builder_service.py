"""
MCP Builder Service — Endpoint discovery and structured extraction.

Searches the product's knowledge base for API endpoints, routes, and handlers,
then uses the configured LLM to extract a structured list of endpoints suitable
for building an MCP server.
"""

import asyncio
import json
import re as _re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.rag.kb_search import search_kb

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class EndpointInfo(BaseModel):
    method: str = Field(..., description="HTTP method (GET, POST, PUT, DELETE, etc.)")
    path: str = Field(..., description="API path (e.g., /v1/customers)")
    description: str = Field(..., description="What this endpoint does")
    suggested_tool_name: str = Field(..., description="Suggested MCP tool name (snake_case, service-prefixed)")
    read_only: bool = Field(default=True, description="True for GET, False for mutations")
    parameters: list[dict] = Field(default_factory=list, description="Request parameters: [{name, type, required, location}]")


class DiscoveryResult(BaseModel):
    base_url: Optional[str] = None
    auth_type: str = "none"
    auth_details: Optional[str] = None
    endpoints: list[EndpointInfo] = Field(default_factory=list)
    pages_crawled: int = 1
    raw_pages: list[tuple[str, str]] = Field(default_factory=list, exclude=True)  # (title, text) for KB storage


class MCPBuilderConfig(BaseModel):
    language: str = Field(default="typescript", description="typescript or python")
    transport: str = Field(default="stdio", description="stdio or http")
    selected_endpoints: list[dict] = Field(default_factory=list)
    output_dir: Optional[str] = None
    api_docs_url: Optional[str] = None
    api_docs_text: Optional[str] = None
    api_base_url: Optional[str] = None
    auth_type: Optional[str] = None
    auth_details: Optional[str] = None


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = """\
You are an API documentation analyst. Given the following knowledge base or external API documentation,
extract ALL API endpoints, routes, or handlers you can find, AND determine the global configuration.

For each endpoint in 'endpoints', provide:
- method: HTTP method (GET, POST, PUT, DELETE, PATCH). If it's a function/handler without a clear method, infer from the name.
- path: The API path or route (e.g., /api/v1/users, /customers/:id).
- description: A concise description of what this endpoint does.
- suggested_tool_name: A snake_case MCP tool name prefixed with the service name (e.g., stripe_create_customer, github_list_repos).
- read_only: true for GET/read operations, false for mutations (POST/PUT/DELETE).
- parameters: An array of request parameters. For each parameter provide: {"name": "field_name", "type": "string|integer|boolean|object|array", "required": true/false, "location": "body|query|path|header"}. Extract EXACT field names from the source code or docs — do NOT guess or rename them.

Respond ONLY with a JSON object containing:
- "base_url": The global base URL (e.g., "https://api.slack.com/web", "https://api.github.com"). Use null if unknown.
- "auth_type": The global authentication pattern. MUST be exactly one of: "bearer", "api_key", "basic", "oauth2", or "none".
- "auth_details": A detailed description of how authentication works. Include:
  - What credentials are needed (API key, app_key + app_secret, username + password, OAuth client_id + client_secret, etc.)
  - What headers to set and their exact names (e.g., "Authorization: Bearer <token>", "X-API-Key: <key>", custom headers)
  - If signing is required (HMAC, SHA256, etc.), describe the signing process
  - If a token exchange or login step is needed before calling APIs, describe it
  - Example: "Requires app_key and app_secret. Sign requests with HMAC-SHA256: signature = hmac(app_secret, timestamp + method + path + body). Send headers: x-app-key, x-signature, x-timestamp."
  - If auth is simple (just a bearer token), say: "Send Authorization: Bearer <token> header."
- "endpoints": A JSON array of the endpoint objects.

No markdown, no explanation, no code fences.

Example output:
{
  "base_url": "https://api.stripe.com/v1",
  "auth_type": "bearer",
  "auth_details": "Send Authorization: Bearer <token> header. Get token from dashboard.",
  "endpoints": [
    {"method": "GET", "path": "/customers", "description": "List all customers", "suggested_tool_name": "stripe_list_customers", "read_only": true, "parameters": [{"name": "limit", "type": "integer", "required": false, "location": "query"}]}
  ]
}

KNOWLEDGE BASE EXCERPTS:
"""


# ---------------------------------------------------------------------------
# MCP Docs KB — index crawled/uploaded docs for agent access
# ---------------------------------------------------------------------------

MCP_PRODUCT_PREFIX = "__mcp__"  # Hidden product prefix — filtered from products list


async def store_mcp_docs_kb(
    server_name: str,
    api_name: str,
    pages: list[tuple[str, str]],
) -> str:
    """Store API docs as a searchable KB (no Product entry created).

    Args:
        server_name: e.g., "webullk"
        api_name: Display name e.g., "Webull"
        pages: List of (title, text) tuples from crawling/upload

    Returns:
        The kb product_id key (for KB tool access).
    """
    from app.rag.kb_store import KnowledgeBaseStore

    product_id = f"{MCP_PRODUCT_PREFIX}{server_name}"

    # Store the KB directly — no Product row needed
    kb_store = KnowledgeBaseStore()
    stats = await kb_store.store_mcp_docs(
        product_id=product_id,
        pages=pages,
        api_name=api_name,
    )

    logger.info("mcp_docs_kb_created", product_id=product_id, server_name=server_name, **stats)
    return product_id


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class MCPBuilderService:
    """Discovers API endpoints from a product's knowledge base."""

    async def discover_endpoints(
        self,
        product_id: str,
        api_key: str,
        model: str = "gpt-4o",
        api_url: Optional[str] = None,
    ) -> list[EndpointInfo]:
        """Search KB and extract structured endpoints using LLM.

        Args:
            product_id: The product to discover endpoints for.
            api_key: LLM API key.
            model: LLM model name.
            api_url: Optional custom LLM API URL.

        Returns:
            List of discovered EndpointInfo objects.
        """

        # ── 1. Search KB for API-related content ──────────────────────
        search_queries = [
            ("API endpoints and routes", ["api", "endpoint", "route", "REST", "handler", "controller", "path", "GET", "POST", "PUT", "DELETE"]),
            ("API functions and methods", ["function", "method", "service", "action", "create", "update", "delete", "list", "get", "fetch"]),
            ("Webhook event handlers and subscriptions", ["webhook", "event", "subscribe", "callback", "notification", "trigger", "on_event"]),
            ("Authentication and authorization endpoints", ["auth", "login", "token", "oauth", "api_key", "bearer", "session", "register"]),
            ("Request response schemas and models", ["schema", "model", "serializer", "dto", "payload", "request_body", "response_model", "BaseModel"]),
        ]

        kb_excerpts: list[str] = []
        for query, keywords in search_queries:
            try:
                result = await search_kb(
                    query=query,
                    product_id=product_id,
                    agent_keywords=keywords,
                    top_k=15,
                )
                if result and "No knowledge base found" not in result and "No relevant information" not in result:
                    kb_excerpts.append(result)
            except Exception as exc:
                logger.warning("mcp_discover_search_failed", query=query, error=str(exc))

        if not kb_excerpts:
            logger.info("mcp_discover_no_kb_results", product_id=product_id)
            return []

        combined = "\n\n---\n\n".join(kb_excerpts)

        # Truncate to avoid exceeding context limits
        if len(combined) > 80_000:
            combined = combined[:80_000] + "\n\n[... truncated ...]"

        # ── 2. Extract endpoints using LLM ──────────────────────────────
        client_kwargs: dict = {"api_key": api_key}
        if api_url:
            client_kwargs["base_url"] = api_url
        client = AsyncOpenAI(**client_kwargs)

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _EXTRACTION_PROMPT},
                    {"role": "user", "content": combined},
                ],
                temperature=0.2,
                max_tokens=8000,
            )
            raw = response.choices[0].message.content or "[]"
        except Exception as exc:
            logger.error("mcp_discover_llm_failed", error=str(exc))
            return []

        # ── 3. Parse response ─────────────────────────────────────────
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            parsed = json.loads(cleaned)
            # Handle old array format fallback
            if isinstance(parsed, list):
                endpoints_raw = parsed
            elif isinstance(parsed, dict) and "endpoints" in parsed:
                endpoints_raw = parsed["endpoints"]
            else:
                endpoints_raw = []
        except json.JSONDecodeError:
            logger.error("mcp_discover_json_parse_failed", raw=raw[:500])
            return []

        if not isinstance(endpoints_raw, list):
            return []

        endpoints: list[EndpointInfo] = []
        for ep in endpoints_raw:
            try:
                endpoints.append(EndpointInfo(**ep))
            except Exception:
                continue

        # ── 4. Deduplicate by (method, path) ──────────────────────────
        seen: set[tuple[str, str]] = set()
        unique: list[EndpointInfo] = []
        for ep in endpoints:
            key = (ep.method.upper(), ep.path)
            if key not in seen:
                seen.add(key)
                unique.append(ep)
        endpoints = unique

        logger.info("mcp_discover_complete", product_id=product_id, count=len(endpoints))
        return endpoints

    # ------------------------------------------------------------------
    # External API discovery (fetch docs from URL)
    # ------------------------------------------------------------------

    async def discover_from_url(
        self,
        api_name: str,
        api_docs_url: str,
        api_key: str,
        model: str = "gpt-4o",
        api_url: Optional[str] = None,
    ) -> DiscoveryResult:
        """Fetch external API docs from a URL and extract endpoints via LLM.

        Works with:
        - OpenAPI / Swagger JSON specs (parsed directly)
        - HTML documentation sites (crawls linked pages, then extracts)
        - Plain text / markdown docs
        """

        # ── 1. Fetch the initial page ─────────────────────────────────
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
                resp = await http.get(api_docs_url, headers={
                    "User-Agent": "ReTrace-MCP-Builder/1.0",
                    "Accept": "application/json, text/html, text/plain, */*",
                })
                resp.raise_for_status()
                raw_content = resp.text
                content_type = resp.headers.get("content-type", "")
        except Exception as exc:
            logger.error("mcp_discover_url_fetch_failed", url=api_docs_url, error=str(exc))
            return DiscoveryResult()

        # ── 2. If it's already JSON/OpenAPI, parse directly (fast path) ──
        is_json = "json" in content_type or raw_content.strip().startswith("{")
        if is_json:
            return await self._parse_documentation(api_name, api_docs_url, raw_content, content_type, api_key, model, api_url)

        # ── 2b. Special Case: GitHub Repository URL ──────────────────────
        # If user provides a GitHub repo link, try to find a spec file in it
        if "github.com" in api_docs_url and "/blob/" not in api_docs_url and "/raw/" not in api_docs_url:
            spec_url = await _find_github_spec(api_docs_url)
            if spec_url:
                logger.info("mcp_discover_github_spec_found", spec_url=spec_url)
                try:
                    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
                        resp = await http.get(spec_url)
                        resp.raise_for_status()
                        return await self._parse_documentation(api_name, spec_url, resp.text, "application/json", api_key, model, api_url)
                except Exception:
                    pass

        # ── 3. HTML page — check for a linked OpenAPI spec first ─────
        if "html" in content_type:
            spec_link = _find_openapi_spec_link(raw_content, api_docs_url)
            if spec_link:
                logger.info("mcp_discover_found_spec_link", spec_url=spec_link)
                try:
                    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
                        spec_resp = await http.get(spec_link, headers={
                            "User-Agent": "ReTrace-MCP-Builder/1.0",
                            "Accept": "application/json, */*",
                        })
                        spec_resp.raise_for_status()
                        spec_content = spec_resp.text
                        spec_ct = spec_resp.headers.get("content-type", "")
                    # Verify it's actually a valid spec
                    try:
                        spec_json = json.loads(spec_content)
                        if isinstance(spec_json, dict) and ("openapi" in spec_json or "swagger" in spec_json):
                            logger.info("mcp_discover_using_linked_spec", spec_url=spec_link)
                            return await self._parse_documentation(api_name, spec_link, spec_content, "application/json", api_key, model, api_url)
                    except json.JSONDecodeError:
                        pass  # Not a valid spec, fall through to crawling
                except Exception:
                    pass  # Failed to fetch spec link, fall through to crawling

            # ── 4. Crawl linked pages to gather comprehensive docs ────
            logger.info("mcp_discover_crawling", base_url=api_docs_url)
            aggregated_text, pages_crawled, raw_pages = await _crawl_api_docs(api_docs_url, raw_content)

            # Use crawled text for LLM extraction
            result = await self._parse_documentation(
                api_name, f"{api_docs_url} (+{pages_crawled - 1} linked pages)",
                aggregated_text, "text/plain",  # Already extracted text
                api_key, model, api_url,
            )
            result.pages_crawled = pages_crawled
            result.raw_pages = raw_pages

            # ── 4b. Low endpoint count? Search for OpenAPI spec on GitHub ──
            if len(result.endpoints) < 10:
                logger.info("mcp_discover_low_count", count=len(result.endpoints), api_name=api_name)
                spec_result = await self._search_openapi_spec(api_name, api_key, model, api_url)
                if spec_result and len(spec_result.endpoints) > len(result.endpoints):
                    logger.info("mcp_discover_spec_found_better",
                                crawl_count=len(result.endpoints),
                                spec_count=len(spec_result.endpoints))
                    # Keep the better result but preserve raw_pages for KB
                    spec_result.pages_crawled = pages_crawled
                    spec_result.raw_pages = raw_pages
                    result = spec_result

            # ── 5. Detect SDK repos and fetch auth source code ────
            try:
                github_links = _find_github_sdk_links(raw_content)
                if github_links:
                    logger.info("sdk_links_found", links=github_links)
                    for gh_url in github_links[:2]:  # Max 2 repos
                        sdk_files = await _fetch_sdk_auth_files(gh_url)
                        if sdk_files:
                            raw_pages.extend(sdk_files)
                            logger.info("sdk_auth_added_to_kb", repo=gh_url, files=len(sdk_files))
            except Exception as exc:
                logger.warning("sdk_fetch_error", error=str(exc))

            # Store crawled docs as a searchable KB
            try:
                safe_name = _re.sub(r'[^a-z0-9]', '_', api_name.lower()).strip('_')
                kb_product_id = await store_mcp_docs_kb(
                    server_name=safe_name,
                    api_name=api_name,
                    pages=raw_pages,
                )
                logger.info("mcp_discover_kb_stored", kb_product_id=kb_product_id)
            except Exception as exc:
                logger.warning("mcp_discover_kb_store_failed", error=str(exc))

            return result

        # ── 5. Plain text / other formats — parse directly ───────────
        return await self._parse_documentation(api_name, api_docs_url, raw_content, content_type, api_key, model, api_url)

    async def _search_openapi_spec(
        self,
        api_name: str,
        api_key: str,
        model: str,
        api_url: Optional[str],
    ) -> Optional[DiscoveryResult]:
        """Search GitHub for a public OpenAPI spec when crawled results are sparse."""
        search_terms = [
            f"{api_name} openapi spec",
            f"{api_name} swagger api-spec",
            f"{api_name} openapi yaml json",
        ]
        logger.info("mcp_discover_searching_spec", api_name=api_name)
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                for term in search_terms:
                    # Search GitHub API for repos with openapi/swagger specs
                    gh_resp = await http.get(
                        "https://api.github.com/search/repositories",
                        params={"q": term, "sort": "stars", "per_page": 3},
                        headers={"Accept": "application/vnd.github.v3+json"},
                    )
                    if gh_resp.status_code != 200:
                        continue
                    repos = gh_resp.json().get("items", [])
                    for repo in repos:
                        full_name = repo.get("full_name", "")
                        spec_url = await _find_github_spec(f"https://github.com/{full_name}")
                        if spec_url:
                            logger.info("mcp_discover_spec_from_github", repo=full_name, spec_url=spec_url)
                            try:
                                spec_resp = await http.get(spec_url, timeout=15.0)
                                spec_resp.raise_for_status()
                                return await self._parse_documentation(
                                    api_name, spec_url, spec_resp.text,
                                    "application/json", api_key, model, api_url,
                                )
                            except Exception:
                                continue
        except Exception as exc:
            logger.warning("mcp_discover_spec_search_failed", error=str(exc))
        return None

    async def discover_from_text(
        self,
        api_name: str,
        api_docs_text: str,
        api_key: str,
        model: str = "gpt-4o",
        api_url: Optional[str] = None,
    ) -> DiscoveryResult:
        """Extract endpoints from directly provided raw text or JSON."""
        content_type = "application/json" if api_docs_text.strip().startswith("{") else "text/plain"
        result = await self._parse_documentation(api_name, "Raw Text Input", api_docs_text, content_type, api_key, model, api_url)

        # Store text in KB so the generation agent can query it
        try:
            raw_pages = [("API Documentation", api_docs_text)]
            safe_name = _re.sub(r'[^a-z0-9]', '_', api_name.lower()).strip('_')
            kb_product_id = await store_mcp_docs_kb(
                server_name=safe_name,
                api_name=api_name,
                pages=raw_pages,
            )
            result.raw_pages = raw_pages
            logger.info("mcp_discover_text_kb_stored", kb_product_id=kb_product_id)
        except Exception as exc:
            logger.warning("mcp_discover_text_kb_store_failed", error=str(exc))

        return result

    async def _parse_documentation(
        self,
        api_name: str,
        source_label: str,
        raw_content: str,
        content_type: str,
        api_key: str,
        model: str,
        api_url: Optional[str],
    ) -> DiscoveryResult:
        # Try deterministic JSON parsing first (OpenAPI/Swagger)
        is_openapi = False
        spec = None
        
        if "json" in content_type or raw_content.strip().startswith("{"):
            try:
                spec = json.loads(raw_content)
                if isinstance(spec, dict) and ("openapi" in spec or "swagger" in spec or "paths" in spec):
                    is_openapi = True
            except json.JSONDecodeError:
                pass

        # ── 3. Path A: Deterministic OpenAPI Parsing (Instant & 100% Accurate) ──
        if is_openapi and isinstance(spec, dict):
            result = DiscoveryResult()
            
            # Detect Base URL from servers block
            servers = spec.get("servers", [])
            if servers and isinstance(servers, list):
                result.base_url = servers[0].get("url")
            elif "host" in spec: # Swagger v2 fallback
                scheme = spec.get("schemes", ["https"])[0]
                host = spec["host"]
                base_path = spec.get("basePath", "")
                result.base_url = f"{scheme}://{host}{base_path}"

            # Detect Auth Type
            security_schemes = spec.get("components", {}).get("securitySchemes", {})
            if not security_schemes:
                security_schemes = spec.get("securityDefinitions", {}) # Swagger v2
            
            auth_type = "none"
            for name, scheme in security_schemes.items():
                t = scheme.get("type", "").lower()
                s = scheme.get("scheme", "").lower()
                if t == "http" and s == "bearer":
                    auth_type = "bearer"
                    break
                elif t == "http" and s == "basic":
                    auth_type = "basic"
                    break
                elif t == "apikey":
                    auth_type = "api_key"
                    break
                elif t == "oauth2":
                    auth_type = "bearer" # Treat oauth2 as bearer token for simplicity
                    break
            result.auth_type = auth_type

            # Parse Endpoints
            paths = spec.get("paths", {})
            for path, methods in paths.items():
                for method, details in methods.items():
                    method_upper = method.upper()
                    if method_upper not in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
                        continue
                    
                    summary = details.get("summary") or details.get("description") or f"{method_upper} {path}"
                    # Keep summary concise
                    summary = summary.split("\n")[0][:100]
                    
                    # Generate a clean snake_case tool name
                    op_id = details.get("operationId", "")
                    if op_id:
                        # Convert op_id to snake_case safely
                        import re as _re
                        clean_op = _re.sub(r'(?<!^)(?=[A-Z])', '_', op_id).lower()
                        clean_op = _re.sub(r'[^a-z0-9_]', '_', clean_op)
                        # Remove duplicate underscores
                        clean_op = _re.sub(r'_+', '_', clean_op).strip('_')
                        tool_name = f"{api_name.lower().replace(' ', '_')}_{clean_op}"
                    else:
                        # Fallback tool name based on path
                        import re as _re
                        clean_path = _re.sub(r'[{}]', '', path).replace('/', '_').strip('_')
                        tool_name = f"{api_name.lower().replace(' ', '_')}_{method.lower()}_{clean_path}"
                    
                    result.endpoints.append(
                        EndpointInfo(
                            method=method_upper,
                            path=path,
                            description=summary,
                            suggested_tool_name=tool_name,
                            read_only=(method_upper == "GET")
                        )
                    )
            
            logger.info("mcp_discover_url_openapi_complete", api_name=api_name, count=len(result.endpoints))
            return result

        # ── 4. Path B: LLM HTML/Text Extraction Fallback ────────────────────────
        doc_text = ""
        if "html" in content_type:
            doc_text = _extract_text_from_html(raw_content)
        else:
            doc_text = raw_content

        if not doc_text.strip():
            logger.info("mcp_discover_empty", source=source_label)
            return DiscoveryResult()

        # No truncation — let the LLM handle the full content.
        # Modern models (GPT-4o, Claude) support 128K+ context windows.

        logger.info(
            "mcp_discover_llm_input",
            source=source_label,
            doc_text_len=len(doc_text),
            doc_text_preview=doc_text[:500],
            doc_text_tail=doc_text[-500:] if len(doc_text) > 500 else "",
        )

        # ── Extract endpoints using LLM ─────────────────────────────
        prompt_text = (
            f"The following is API documentation for '{api_name}'. "
            f"Source: {source_label}\n\n"
            f"{doc_text}"
        )

        client_kwargs: dict = {"api_key": api_key}
        if api_url:
            client_kwargs["base_url"] = api_url
        client = AsyncOpenAI(**client_kwargs)

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _EXTRACTION_PROMPT},
                    {"role": "user", "content": prompt_text},
                ],
                temperature=0.2,
                max_tokens=8000,
            )
            raw = response.choices[0].message.content or "[]"
        except Exception as exc:
            logger.error("mcp_discover_url_llm_failed", error=str(exc))
            return DiscoveryResult()

        logger.info(
            "mcp_discover_llm_raw_response",
            raw_len=len(raw),
            raw_preview=raw[:1000],
        )

        # ── Parse response ──────────────────────────────────────────
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        result = DiscoveryResult()

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                result.base_url = parsed.get("base_url")
                result.auth_type = parsed.get("auth_type", "none")
                result.auth_details = parsed.get("auth_details")
                endpoints_raw = parsed.get("endpoints", [])
            elif isinstance(parsed, list):
                # Fallback if LLM just returns array
                endpoints_raw = parsed
            else:
                endpoints_raw = []
        except json.JSONDecodeError:
            logger.error("mcp_discover_url_json_parse_failed", raw=raw[:500])
            return result

        if not isinstance(endpoints_raw, list):
            logger.warning("mcp_discover_not_list", type=type(endpoints_raw).__name__)
            return result

        logger.info("mcp_discover_parsed", endpoints_count=len(endpoints_raw), parsed_type=type(parsed).__name__)

        endpoints: list[EndpointInfo] = []
        for ep in endpoints_raw:
            try:
                endpoints.append(EndpointInfo(**ep))
            except Exception as ep_err:
                logger.warning("mcp_discover_endpoint_parse_failed", endpoint=str(ep)[:200], error=str(ep_err))
                continue

        # Deduplicate by (method, path)
        seen: set[tuple[str, str]] = set()
        unique: list[EndpointInfo] = []
        for ep in endpoints:
            key = (ep.method.upper(), ep.path)
            if key not in seen:
                seen.add(key)
                unique.append(ep)
        endpoints = unique

        result.endpoints = endpoints
        logger.info("mcp_discover_llm_complete", api_name=api_name, count=len(endpoints))
        return result

    def slice_openapi_for_endpoints(self, raw_openapi: str, selected_endpoints: list[dict]) -> Optional[str]:
        """
        Takes a massive OpenAPI JSON string and slices out ONLY the components and paths 
        needed for the precise endpoints the user checked, reducing context size dramatically.
        """
        try:
            spec = json.loads(raw_openapi)
            if not isinstance(spec, dict) or ("openapi" not in spec and "swagger" not in spec):
                return None
        except Exception:
            return None

        # Build a fast lookup of selected (path, method) combos
        selected_matrix = {}
        for ep in selected_endpoints:
            path = ep.get("path")
            method = ep.get("method", "").lower()
            if path and method:
                selected_matrix.setdefault(path, set()).add(method)

        sliced_spec = {}
        
        # Copy high-level metadata needed for context
        for key in ["openapi", "swagger", "info", "servers", "host", "basePath", "schemes"]:
            if key in spec:
                sliced_spec[key] = spec[key]

        # Slice Paths
        sliced_paths = {}
        paths_in = spec.get("paths", {})
        for path, methods_obj in paths_in.items():
            if path in selected_matrix:
                sliced_methods = {}
                for method, details in methods_obj.items():
                    if method.lower() in selected_matrix[path]:
                        sliced_methods[method] = details
                if sliced_methods:
                    sliced_paths[path] = sliced_methods
        
        sliced_spec["paths"] = sliced_paths

        # Discover all $refs recursively
        collected_refs: set[str] = set()
        
        def _trace_refs(obj: Any):
            if isinstance(obj, dict):
                if "$ref" in obj and isinstance(obj["$ref"], str):
                    collected_refs.add(obj["$ref"])
                for v in obj.values():
                    _trace_refs(v)
            elif isinstance(obj, list):
                for item in obj:
                    _trace_refs(item)
                    
        _trace_refs(sliced_paths)

        # Slice Components/Definitions based on traced refs
        resolved_refs = set()
        
        def _resolve_and_trace(ref: str):
            if ref in resolved_refs:
                return
            resolved_refs.add(ref)
            
            parts = ref.split("/")
            if len(parts) >= 4 and parts[0] == "#" and parts[1] in ["components", "definitions"]:
                # Traverse the spec to find the component
                current = spec
                for part in parts[1:]:
                    if isinstance(current, dict) and part in current:
                        current = current[part]
                    else:
                        return
                # Trace any refs inside the resolved component
                _trace_refs(current)

        # Process refs repeatedly until no new ones are found (resolves nested schemas)
        while True:
            unresolved = collected_refs - resolved_refs
            if not unresolved:
                break
            for r in list(unresolved):
                _resolve_and_trace(r)

        # Copy over the exact components that were referenced
        if "components" in spec:
            sliced_spec["components"] = {"securitySchemes": spec["components"].get("securitySchemes", {})}
            for ref in resolved_refs:
                parts = ref.split("/")
                if len(parts) == 4 and parts[1] == "components":
                    category, name = parts[2], parts[3]
                    if category not in sliced_spec["components"]:
                        sliced_spec["components"][category] = {}
                    if category in spec["components"] and name in spec["components"][category]:
                        sliced_spec["components"][category][name] = spec["components"][category][name]
        
        if "definitions" in spec: # Swagger v2
            sliced_spec["definitions"] = {}
            for ref in resolved_refs:
                parts = ref.split("/")
                if len(parts) == 3 and parts[1] == "definitions":
                    name = parts[2]
                    if name in spec["definitions"]:
                        sliced_spec["definitions"][name] = spec["definitions"][name]

        return json.dumps(sliced_spec, indent=2)


def _extract_text_from_html(html: str) -> str:
    """Crude HTML-to-text extraction (no external dependency needed)."""
    import re as _re

    # Remove script and style blocks
    text = _re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=_re.DOTALL | _re.IGNORECASE)
    # Replace <br>, <p>, <div>, <li> with newlines
    text = _re.sub(r"<(br|/p|/div|/li|/tr|/h[1-6])[^>]*>", "\n", text, flags=_re.IGNORECASE)
    # Strip all remaining tags
    text = _re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = _re.sub(r"[ \t]+", " ", text)
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Web Crawler for HTML Documentation Sites
# ---------------------------------------------------------------------------

# Links containing these keywords are likely API doc pages worth crawling
_API_LINK_KEYWORDS = {
    "api", "method", "endpoint", "reference", "resource", "docs",
    "guide", "rest", "graphql", "webhook", "event", "auth",
    "operation", "route", "path", "service", "sdk", "client",
}

# Links containing these keywords should be skipped
_SKIP_KEYWORDS = {
    "login", "signup", "sign-up", "register", "pricing", "blog",
    "changelog", "status", "support", "contact", "terms", "privacy",
    "cookie", "careers", "about", "community", "forum", "twitter",
    "github.com", "linkedin", "youtube", "facebook", "instagram",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".css", ".js",
    ".woff", ".ttf", ".eot", ".pdf", ".zip", ".tar", ".gz",
    "mailto:", "javascript:", "#",
}


def _find_github_sdk_links(html: str) -> list[str]:
    """Find GitHub repository links from an HTML page (likely SDK repos)."""
    hrefs = _re.findall(r'href=["\']([^"\']+)["\']', html, _re.IGNORECASE)
    github_repos = set()
    for href in hrefs:
        # Match github.com/org/repo patterns
        match = _re.match(r'https?://github\.com/([^/]+/[^/]+?)(?:/.*)?$', href.strip())
        if match:
            repo = match.group(1).rstrip("/")
            # Skip common non-SDK repos
            if not any(skip in repo.lower() for skip in ["awesome-", ".github.io", "blog"]):
                github_repos.add(f"https://github.com/{repo}")
    return list(github_repos)


async def _fetch_sdk_auth_files(github_url: str) -> list[tuple[str, str]]:
    """Fetch auth/signing related source files from a GitHub repo.

    Uses GitHub API to find files related to auth/signing, then fetches their content.
    Returns list of (file_path, file_content) tuples.
    """
    # Extract owner/repo from URL
    match = _re.match(r'https?://github\.com/([^/]+/[^/]+)', github_url)
    if not match:
        return []

    repo = match.group(1)

    # Auth-related keywords for file paths
    auth_keywords = {"auth", "sign", "hmac", "credential", "token", "security", "client", "signer", "oauth"}
    # Skip test files, vendor files, large files
    skip_patterns = {"test_", "tests/", "vendor", "node_modules", "__pycache__", ".min.js"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            # Get repo file tree
            # Try 'main' branch first, fall back to 'master'
            tree_resp = None
            for branch in ["main", "master"]:
                resp = await http.get(
                    f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1",
                    headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "ReTrace-MCP-Builder/1.0"},
                )
                if resp.status_code == 200:
                    tree_resp = resp
                    break

            if not tree_resp:
                logger.warning("sdk_tree_fetch_failed", repo=repo)
                return []

            tree_data = tree_resp.json()
            files = tree_data.get("tree", [])

            # Filter to auth-related Python/JS/TS files
            auth_files = []
            for f in files:
                if f.get("type") != "blob":
                    continue
                path = f["path"].lower()
                # Must be a source file
                if not any(path.endswith(ext) for ext in (".py", ".js", ".ts", ".java")):
                    continue
                # Skip test/vendor files
                if any(skip in path for skip in skip_patterns):
                    continue
                # Must contain auth keyword
                if any(kw in path for kw in auth_keywords):
                    auth_files.append(f["path"])

            # Cap at 15 files to avoid rate limits
            auth_files = auth_files[:15]

            if not auth_files:
                logger.info("sdk_no_auth_files", repo=repo)
                return []

            logger.info("sdk_auth_files_found", repo=repo, count=len(auth_files))

            # Fetch each file's content via raw.githubusercontent.com
            results: list[tuple[str, str]] = []
            branch_name = "main"  # Use whatever branch worked
            for file_path in auth_files:
                try:
                    raw_url = f"https://raw.githubusercontent.com/{repo}/{branch_name}/{file_path}"
                    content_resp = await http.get(raw_url)
                    if content_resp.status_code == 200:
                        content = content_resp.text
                        # Skip very large files (>50KB)
                        if len(content) < 50000:
                            results.append((f"sdk/{repo}/{file_path}", content))
                except Exception:
                    continue

            logger.info("sdk_auth_files_fetched", repo=repo, fetched=len(results))
            return results

    except Exception as exc:
        logger.warning("sdk_fetch_failed", repo=repo, error=str(exc))
        return []


def _find_openapi_spec_link(html: str, base_url: str) -> Optional[str]:
    """Scan HTML for a link to an OpenAPI/Swagger spec file."""
    # Find all href values
    hrefs = _re.findall(r'href=["\']([^"\']+)["\']', html, _re.IGNORECASE)

    spec_keywords = {"openapi", "swagger", "api-spec", "api_spec", "apispec"}

    for href in hrefs:
        href_lower = href.lower()
        # Check if it looks like a spec file
        is_spec_ext = href_lower.endswith((".json", ".yaml", ".yml"))
        has_spec_keyword = any(kw in href_lower for kw in spec_keywords)

        if is_spec_ext and has_spec_keyword:
            return urljoin(base_url, href)

    return None


async def _find_github_spec(repo_url: str) -> Optional[str]:
    """Try to find an OpenAPI/Swagger spec in a GitHub repository."""
    # Convert 'https://github.com/user/repo' to 'https://raw.githubusercontent.com/user/repo/master/'
    match = _re.match(r"https://github\.com/([^/]+)/([^/]+)(/tree/([^/]+))?", repo_url)
    if not match:
        return None
    
    user, repo = match.group(1), match.group(2)
    branch = match.group(4) or "master" # Try master then main
    
    common_specs = [
        "openapi.json", "openapi.yaml", "openapi.yml",
        "swagger.json", "swagger.yaml", "swagger.yml",
        "api-spec.json", "api_spec.json",
        "docs/openapi.json", "docs/swagger.json",
        "web-api/slack_web_openapi_v2.json" # Special case for user's example
    ]

    for b in [branch, "main", "develop"]:
        base_raw = f"https://raw.githubusercontent.com/{user}/{repo}/{b}/"
        async with httpx.AsyncClient(timeout=5.0) as http:
            for spec_path in common_specs:
                try:
                    url = urljoin(base_raw, spec_path)
                    resp = await http.head(url)
                    if resp.status_code == 200:
                        return url
                except Exception:
                    continue
    return None


def _extract_links_from_html(html: str, base_url: str) -> list[str]:
    """Extract and filter API-relevant links from an HTML page."""
    hrefs = _re.findall(r'href=["\']([^"\']+)["\']', html, _re.IGNORECASE)

    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc.lower()

    seen: set[str] = set()
    relevant_links: list[str] = []

    for href in hrefs:
        # Skip obviously irrelevant links
        href_lower = href.lower().strip()
        if any(skip in href_lower for skip in _SKIP_KEYWORDS):
            continue

        # Resolve relative URLs
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        # Same domain only
        if parsed.netloc.lower() != base_domain:
            continue

        # Normalize: remove fragment, trailing slash
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        if normalized in seen or normalized == base_url.rstrip("/"):
            continue
        seen.add(normalized)

        # Check if the path looks API-relevant
        path_lower = parsed.path.lower()
        is_relevant = any(kw in path_lower for kw in _API_LINK_KEYWORDS)

        if is_relevant:
            relevant_links.append(full_url)

    return relevant_links


async def _crawl_api_docs(
    base_url: str,
    initial_html: str,
    max_pages: int = 50,
    max_depth: int = 2,
) -> tuple[str, int, list[tuple[str, str]]]:
    """
    Crawl linked pages from an API documentation site with multi-depth support.

    Many doc sites have: index → category pages → actual endpoint pages.
    This crawler follows links up to `max_depth` levels deep to reach the
    actual endpoint documentation.

    Returns (aggregated_text, pages_crawled_count).
    """
    semaphore = asyncio.Semaphore(10)
    visited: set[str] = set()
    # Normalize the base URL for dedup
    visited.add(base_url.rstrip("/"))

    # Store results: (url, extracted_text)
    all_pages: list[tuple[str, str]] = []

    async def fetch_page(url: str) -> Optional[tuple[str, str, str]]:
        """Fetch a page. Returns (url, extracted_text, raw_html) or None."""
        async with semaphore:
            try:
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as http:
                    resp = await http.get(url, headers={
                        "User-Agent": "ReTrace-MCP-Builder/1.0",
                        "Accept": "text/html, */*",
                    })
                    resp.raise_for_status()
                    raw_html = resp.text
                    content_type = resp.headers.get("content-type", "")
                    if "html" in content_type:
                        text = _extract_text_from_html(raw_html)
                    else:
                        text = raw_html
                        raw_html = ""  # Not HTML, can't extract links
                    # Skip very short pages (likely redirects or error pages)
                    if len(text.strip()) < 100:
                        return None
                    return (url, text, raw_html)
            except Exception:
                return None

    # ── Depth 1: Links from the initial page ──────────────────────
    depth1_links = _extract_links_from_html(initial_html, base_url)

    if not depth1_links:
        logger.info("mcp_crawl_no_links", base_url=base_url)
        initial_text = _extract_text_from_html(initial_html)
        return initial_text, 1, [(base_url, initial_text)]

    logger.info("mcp_crawl_depth1_start", base_url=base_url, links_found=len(depth1_links))

    # Fetch depth-1 pages
    depth1_links = depth1_links[:max_pages]
    for url in depth1_links:
        visited.add(url.rstrip("/"))

    depth1_results = await asyncio.gather(*[fetch_page(url) for url in depth1_links])

    depth2_candidate_links: list[str] = []

    for r in depth1_results:
        if r is None:
            continue
        url, text, raw_html = r
        all_pages.append((url, text))

        # Extract links from this page for depth-2 crawling
        if max_depth >= 2 and raw_html:
            sub_links = _extract_links_from_html(raw_html, url)
            for link in sub_links:
                normalized = link.rstrip("/")
                if normalized not in visited:
                    visited.add(normalized)
                    depth2_candidate_links.append(link)

    # ── Depth 2: Links discovered from depth-1 pages ─────────────
    if depth2_candidate_links and max_depth >= 2:
        # Budget remaining pages
        remaining_budget = max_pages - len(all_pages)
        depth2_links = depth2_candidate_links[:max(remaining_budget, 0)]

        if depth2_links:
            logger.info("mcp_crawl_depth2_start", new_links=len(depth2_links))

            depth2_results = await asyncio.gather(*[fetch_page(url) for url in depth2_links])

            for r in depth2_results:
                if r is None:
                    continue
                url, text, _ = r
                all_pages.append((url, text))

    # ── Assemble aggregated text ─────────────────────────────────
    # Prioritize deeper pages (actual endpoint docs) over shallow ones
    # (category indices). Deeper pages have longer paths and more content.
    # Sort by path depth (more segments = deeper = more likely endpoint docs)
    # then by text length descending (longer = richer content).
    all_pages.sort(
        key=lambda p: (-p[0].count("/"), -len(p[1])),
    )

    initial_text = _extract_text_from_html(initial_html)
    # Start with a brief header, not the full initial page (often just nav/intro)
    parts = [f"--- Page: {base_url} (index) ---\n\n{initial_text[:2000]}"]

    for url, text in all_pages:
        parts.append(f"--- Page: {url} ---\n\n{text}")

    aggregated = "\n\n".join(parts)
    pages_crawled = 1 + len(all_pages)

    logger.info(
        "mcp_crawl_complete",
        base_url=base_url,
        pages_crawled=pages_crawled,
        depth1=len([r for r in depth1_results if r]),
        depth2=len(all_pages) - len([r for r in depth1_results if r]),
        total_chars=len(aggregated),
    )

    # Build raw pages list for KB storage
    raw_pages = [(base_url, initial_text)]
    for url, text in all_pages:
        raw_pages.append((url, text))

    return aggregated, pages_crawled, raw_pages


def format_endpoints_as_brief(config: MCPBuilderConfig, product_name: str = "the product") -> str:
    """Format selected endpoints into a structured task brief for the agent."""
    lang_label = "TypeScript" if config.language == "typescript" else "Python"
    transport_label = "stdio (local)" if config.transport == "stdio" else "HTTP (remote)"

    lines = [
        f"Build a {lang_label} MCP server ({transport_label} transport) for {product_name} with these {len(config.selected_endpoints)} tools:",
        "",
    ]
    for i, ep in enumerate(config.selected_endpoints, 1):
        method = ep.get("method", "?")
        path = ep.get("path", "?")
        desc = ep.get("description", "")
        tool_name = ep.get("suggested_tool_name", "unknown_tool")
        lines.append(f"{i}. `{tool_name}` — {method} {path} — {desc}")

    return "\n".join(lines)


# Global instance
mcp_builder_service = MCPBuilderService()
