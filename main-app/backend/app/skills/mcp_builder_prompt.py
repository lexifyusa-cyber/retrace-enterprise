"""
MCP Builder skill prompt supplement.

Reads the vendored Anthropic MCP Builder reference files and produces a
system-prompt supplement that the ReTrace CodeAct agent can follow to
scaffold, implement, and validate a complete MCP server for the user's
product.
"""

from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_SKILL_DIR = Path(__file__).resolve().parent / "mcp-builder"

_REFERENCE_FILES = {
    "best_practices": _SKILL_DIR / "mcp_best_practices.md",
    "typescript": _SKILL_DIR / "node_mcp_server.md",
    "python": _SKILL_DIR / "python_mcp_server.md",
    "evaluation": _SKILL_DIR / "evaluation.md",
}

_cache: dict[str, str] = {}


def _read_ref(key: str) -> str:
    """Read and cache a reference markdown file."""
    if key not in _cache:
        path = _REFERENCE_FILES[key]
        try:
            _cache[key] = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("mcp_builder_ref_missing", key=key, path=str(path))
            _cache[key] = ""
    return _cache[key]


_MCP_BUILDER_WORKFLOW = """\
# MCP BUILDER MODE

You are now operating in **MCP Builder** mode. Your job is to build a
complete, production-quality MCP (Model Context Protocol) server for the
user's application.

## Working Directory

All files MUST be written inside the output directory: `{output_dir}`
Start by running `terminal("cd {output_dir}")` so that all relative paths
resolve correctly. If the directory does not exist, create it first with
`terminal("mkdir -p {output_dir}")`.

## Language

Generate a **{language}** MCP server. Follow the {language_label} implementation
guide below exactly.

## Four-Phase Workflow

### Phase 1 — Understand the Product (6 Mandatory Queries)

You MUST run ALL 6 queries below in order. Do NOT skip any. Each query targets
a specific category of information required to generate correct code.

**Query 1 — Route Handlers (exact field names + types):**
```
knowledge_base(action="search",
  query="route handler function signatures request body parameters",
  keywords=["def ", "async def", "@router", "@app", "BaseModel", "Schema",
  "request", "body", "params", "Field(", "Query(", "Path("])
```
This finds Pydantic models, function parameters, and dataclasses — giving you
the EXACT field names (e.g., `question` not `query`, `product_ids` not `product_id`).

**Query 2 — Global Route Mounting (prefix discovery):**
```
knowledge_base(action="search",
  query="router mounting app include_router prefix API versioning",
  keywords=["include_router", "prefix=", "/api/v", "app.mount", "Blueprint",
  "urlpatterns", "APIRouter", "Router("])
```
This finds lines like `app.include_router(router, prefix="/api/v1")` — giving
you the exact global route prefix. Store it as `API_PREFIX` in Phase 2.

**Query 3 — Authentication Middleware (auth header format):**
```
knowledge_base(action="search",
  query="authentication middleware authorization header API key token verification",
  keywords=["Authorization", "Bearer", "X-API-Key", "api_key", "token",
  "authenticate", "get_current_user", "Depends", "middleware", "BasicAuth"])
```
This reveals whether the API uses Bearer JWT, API Key, Basic Auth, or cookies.

**Query 4 — Error Response Patterns:**
```
knowledge_base(action="search",
  query="error handling HTTP exceptions error response format",
  keywords=["HTTPException", "status_code", "detail", "error response",
  "ValidationError", "400", "401", "403", "404", "422", "500"])
```

**Query 5 — Response Models (output field names):**
```
knowledge_base(action="search",
  query="response model return type serialization",
  keywords=["response_model", "return", "to_dict", "model_dump", "jsonify",
  "ResponseModel", "Serializer", "schema"])
```

**Query 6 — Deep Dive (read the actual source):**
After Queries 1-5, use `knowledge_base(action="read_file", file_path="<path>")`
on the **top 3 most relevant source files** found during search. Read the actual
code to extract exact function signatures, decorators, and parameter types.
This eliminates ALL guessing.

**After all 6 queries:**
- If the user mentioned external API documentation URLs, fetch them with `web_fetch(url)`.
- Draft a **tool plan**: list every MCP tool the server should expose, with
  names (snake_case, service-prefixed), descriptions, and EXACT parameter names
  as discovered from the KB.
- Present the tool plan to the user and wait for confirmation before coding.

### Phase 2 — Scaffold and Implement

1. Create the project skeleton in `{output_dir}` using `write_file`:
   - For TypeScript: package.json, tsconfig.json, src/index.ts, src/types.ts,
     src/tools/, src/services/, README.md
   - For Python: pyproject.toml, src/{{service}}_mcp/__init__.py,
     src/{{service}}_mcp/server.py, README.md
   - The `pyproject.toml` MUST include a `[build-system]` section:
     ```
     [build-system]
     requires = ["setuptools>=61"]
     build-backend = "setuptools.build_meta"
     ```
2. Implement each tool following the reference guide conventions:
   - Input validation with Zod (TS) or Pydantic (Python)
   - Tool annotations (readOnlyHint, destructiveHint, idempotentHint, openWorldHint)
   - Comprehensive docstrings / descriptions
   - Shared API client and error-handling helpers
   - Pagination support where applicable
   - TypeScript: server name `{{service}}-mcp-server`, tool names `{{service}}_action_resource`
   - Python: server name `{{service}}_mcp`, tool names `{{service}}_action_resource`

3. **CRITICAL PATH PREFIX RULE:**
   From Phase 1 Query 2, you discovered the global route prefix (e.g., `/api/v1`).
   You MUST:
   - Store it as a constant: `API_PREFIX = "/api/v1"` (or whatever you found)
   - In the shared API client `request()` method, prepend `API_PREFIX` to every endpoint
   - All tool functions pass ONLY the route-local path (e.g., `/query`, NOT `/api/v1/query`)
   - This prevents duplication and ensures correctness for every product

4. **CRITICAL AUTH RULE:**
   From Phase 1 Query 3, you discovered the exact auth pattern. Implement it:
   - If Bearer JWT: `headers["Authorization"] = f"Bearer {{token}}"`
   - If API Key in header: use the EXACT header name from the source code (e.g., `X-API-Key`, `X-Auth-Token`, `Api-Key`). Do NOT assume `Authorization: Bearer` if the code uses a different header.
   - If API Key in query param: `params["api_key"] = api_key` (use the exact param name from docs)
   - If Basic Auth: use `httpx.BasicAuth(username, password)` with two env vars: `{{SERVICE}}_USERNAME` and `{{SERVICE}}_PASSWORD`
   - If OAuth2 Client Credentials: implement a token exchange helper that POSTs to the token endpoint and caches the access token
   - If custom header: read the EXACT header name and format from source — never guess
   - If no auth found: make auth optional with a clear env var
   - Use a SINGLE env var name `{{SERVICE}}_API_TOKEN` in BOTH the server code
     AND the Phase 4 JSON. They MUST match exactly.
   - IMPORTANT: Read the ACTUAL auth implementation from the source code or docs.
     Do NOT default to "Authorization: Bearer" without evidence.

5. **CRITICAL TIMEOUT RULE:**
   Set the `httpx.AsyncClient` timeout to `120.0` seconds. Many APIs (especially
   those with LLM/RAG backends) take 30-60s to respond. A 30s default causes
   false timeouts.

### Phase 3 — Build and Validate

You MUST run ALL validation steps below IN ORDER. Do not skip any. Fix errors before proceeding to the next step.

1. **Install dependencies**: run `terminal("cd {output_dir} && uv sync")`
   — this creates a proper `.venv` with all dependencies. If `uv sync` fails, fix pyproject.toml and retry.

2. **Syntax check**: run `terminal("cd {output_dir} && .venv/bin/python -m py_compile src/*/server.py")`
   — if syntax errors exist, fix them and re-run until clean.

3. **Import check**: run `terminal("cd {output_dir} && .venv/bin/python -c \\"from {{service}}_mcp.server import mcp; print('Import OK')\\")")`
   — this catches missing imports, undefined names, Pydantic schema errors, and module structure issues.
   If this fails, read the error carefully, fix the code, and re-run until it prints "Import OK".

4. **Server startup test**: run `terminal("cd {output_dir} && timeout 5 .venv/bin/python -m {{service}}_mcp.server 2>&1 || true")`
   — a timeout exit (code 124) means the server started successfully and is waiting for connections.
   If the server crashes on startup (non-timeout error), fix the error and re-run.

5. **Tool count verification**: run `terminal("cd {output_dir} && .venv/bin/python -c \\"from {{service}}_mcp.server import mcp; print(f'Registered tools: {{len(mcp._tool_manager._tools)}}')\\")")`
   — the count MUST match the number of selected endpoints. If tools are missing, go back and implement them.

6. **Generate README.md** with:
   - What the MCP server does
   - Prerequisites (Python version, API keys/tokens)
   - Install and run instructions (`uv sync`, then run command)
   - List of all tools with brief descriptions

**CRITICAL: Do NOT proceed to Phase 4 until ALL steps pass.** If any step fails after 3 fix attempts, proceed but note the issue in your final answer.

### Phase 4 — Deliver

1. Output a summary of everything created: file tree, tool count, transport mode.
2. Provide a ready-to-paste MCP client config snippet in your final text response. You MUST format it as a markdown JSON block so the UI can parse it:
   ```json
   {{
     "mcpServers": {{
       "{{service}}": {{
         "command": "{output_dir}/.venv/bin/python",
         "args": ["-m", "{{service}}_mcp.server"],
         "env": {{ "{{SERVICE}}_API_TOKEN": "<your-token>" }}
       }}
     }}
   }}
   ```
   IMPORTANT: The `command` MUST be the absolute path to the `.venv/bin/python`
   that was created by `uv sync` in Phase 3. This ensures the server always
   runs from the live source code, not a stale cached copy.
   The env var name MUST match the one used in the server code (Phase 2 Step 4).

3. **REQUIRED: Quick Start Commands for the user.**
   After the JSON config block, you MUST also output a clearly labeled
   "Quick Start" section with exact terminal commands the user can copy-paste.
   Format it as a markdown bash block. The commands must include:

   ```bash
   # Step 1: Install dependencies (only needed once)
   cd {output_dir}
   uv sync

   # Step 2: Start the MCP server
   {output_dir}/.venv/bin/python -m {{service}}_mcp.server

   # Step 3 (optional): Test with MCP Inspector
   npx @modelcontextprotocol/inspector {output_dir}/.venv/bin/python -m {{service}}_mcp.server
   ```

   This is critical because many users are non-technical and need exact
   commands they can paste into their terminal without modification.

4. Suggest testing with `npx @modelcontextprotocol/inspector` (TS) or `mcp dev src/<service>_mcp/server.py` (Python).

## Important Rules

- Use `todo_write(...)` to plan and track your progress across phases.
- **CRITICAL FastMCP Rule:** The `report_progress` function signature ONLY takes numbers (e.g., `report_progress(0.5)`). If you need to log text strings, you MUST use `ctx.info("msg")` instead. NEVER pass a string to `report_progress` or Pydantic will crash with a runtime ValidationError!
- **Code execution:** Put all tool calls in a single **```python** fenced block per step.
  Assign tool results to variables, then **`print(...)` those values inside the same block**
  so you can see output. Never emit bare `print(...)` or other Python **outside** the fence.
- **File Writing:** When using the `write_file(filename, content)` tool, you MUST pass the file content as a direct multi-line string literal. Do NOT pass undefined variable names.
- **CRITICAL ANTI-LAZINESS RULE:** You are building an automated script. DO NOT output source code into the chat response (`Agent Answer`) for the user to copy. You MUST write all code to the disk using the `write_file` tool inside a ````python` block.
- Do NOT skip Phase 1 — understanding the product is critical to generating
  relevant tools.
- If builds fail, read the error output carefully and fix the code.
- Prefer `str_replace(file, old, new)` for targeted fixes over rewriting
  entire files with `write_file`.
- Keep tool descriptions concise but informative.

---

## Reference: MCP Best Practices

{best_practices}

---

## Reference: {language_label} Implementation Guide

{language_guide}
"""
_EXTERNAL_API_BLOCK_URL = """

---

## EXTERNAL API MODE

This MCP server targets an EXTERNAL third-party API, not a local product.

- API Documentation: {api_docs_url}
- Base URL: {api_base_url}
- Auth Type: {auth_type}

**CRITICAL RULES FOR EXTERNAL APIs:**

1. **SKIP ALL Phase 1 knowledge_base queries.** They do not apply.
   Instead, use `web_fetch("{api_docs_url}")` to read the API documentation.
   Parse the response to understand routes, request bodies, and auth patterns.
2. The shared API client base URL MUST be set to: `{api_base_url}`
3. The `API_PREFIX` constant must be derived from the external docs (it may be
   empty `""` if routes already include their full path).
4. Auth type is `{auth_type}`:
   - `bearer`: `headers["Authorization"] = f"Bearer {{token}}"`
   - `api_key`: `headers["X-API-Key"] = api_key`
   - `basic`: `httpx.BasicAuth(username, password)`
   - `none`: no auth headers required
5. The environment variable for the token should be `{{SERVICE}}_API_TOKEN`.
6. Because this is an external API, do NOT assume localhost. The base URL
   above is the production endpoint.
7. DO NOT use `lifespan` hooks or `request_context.lifespan_state`. Just initialize `mcp = FastMCP("server_name")`. External APIs are stateless and just use `httpx.AsyncClient()` inside the tools.
8. ALWAYS strip common placeholder characters from the token after loading, like: `token = os.getenv("VAR").strip().strip("<>").strip('"').strip("'")`.
"""

_EXTERNAL_API_BLOCK_TEXT = """

---

## EXTERNAL API MODE

This MCP server targets an EXTERNAL third-party API, not a local product.

- Base URL: {api_base_url}
- Auth Type: {auth_type}

**CRITICAL RULES FOR EXTERNAL APIs:**

1. **SKIP ALL Phase 1 knowledge_base queries.** They do not apply.
   You MUST use the exact API schema documentation provided below to understand routes, request bodies, and auth patterns.
   DO NOT make up any parameters. Use exactly what is documented below:
   
<api_documentation>
{api_docs_text}
</api_documentation>

2. The shared API client base URL MUST be set to: `{api_base_url}`
3. The `API_PREFIX` constant must be derived from the external docs (it may be
   empty `""` if routes already include their full path).
4. Auth type is `{auth_type}`:
   - `bearer`: `headers["Authorization"] = f"Bearer {{token}}"`
   - `api_key`: `headers["X-API-Key"] = api_key`
   - `basic`: `httpx.BasicAuth(username, password)`
   - `none`: no auth headers required
5. The environment variable for the token should be `{{SERVICE}}_API_TOKEN`.
6. Because this is an external API, do NOT assume localhost. The base URL
   above is the production endpoint.
7. DO NOT use `lifespan` hooks or `request_context.lifespan_state`. Just initialize `mcp = FastMCP("server_name")`. External APIs are stateless and just use `httpx.AsyncClient()` inside the tools.
8. ALWAYS strip common placeholder characters from the token after loading, like: `token = os.getenv("VAR").strip().strip("<>").strip('"').strip("'")`.
"""


# ---------------------------------------------------------------------------
# Slim MCP Builder Prompt (~6K chars instead of 58K)
# ---------------------------------------------------------------------------

_SLIM_SERVER_TEMPLATE = '''import os
from typing import Optional
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("{service}_mcp")

API_BASE_URL = "{base_url}"

def _get_headers() -> dict:
    """Build auth headers."""
    headers = {{"Content-Type": "application/json", "Accept": "application/json"}}
    {auth_code}
    return headers

async def _request(method: str, path: str, params: dict = None, json_body: dict = None) -> dict:
    """Shared HTTP client."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.request(method, f"{{API_BASE_URL}}{{path}}", params=params, json=json_body, headers=_get_headers())
        resp.raise_for_status()
        return resp.json() if resp.content else {{"status": "ok"}}

# --- Add @mcp.tool() functions below, one per endpoint ---
'''

_SLIM_PYPROJECT_TEMPLATE = '''[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "{service}-mcp"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["mcp>=1.0", "httpx>=0.27", "pydantic>=2.0"]

[project.scripts]
{service}-mcp = "{service}_mcp.server:main"
'''

_AUTH_SNIPPETS = {
    "bearer": 'token = os.environ.get("{SERVICE}_API_TOKEN", "").strip()\n    if token:\n        headers["Authorization"] = f"Bearer {{token}}"',
    "api_key": 'key = os.environ.get("{SERVICE}_API_KEY", "").strip()\n    if key:\n        headers["X-API-Key"] = key',
    "basic": 'import base64\n    user = os.environ.get("{SERVICE}_USERNAME", "")\n    pw = os.environ.get("{SERVICE}_PASSWORD", "")\n    if user:\n        headers["Authorization"] = "Basic " + base64.b64encode(f"{{user}}:{{pw}}".encode()).decode()',
    "none": '# No authentication required',
}

_SLIM_EXTERNAL = """\
# MCP BUILDER MODE

You are building a Python MCP server at `{output_dir}`.
You have these native tools available: `write_file`, `read_file`, `str_replace`, `delete_file`, `terminal`, `knowledge_base`, `web_search`, `web_fetch`.
Call them directly as tool calls — do NOT write Python code blocks.

## Step 1: Create project files

Call write_file for each of these files:

**File 1: `{output_dir}/pyproject.toml`**
Content:
{pyproject}

**File 2: `{output_dir}/src/{service}_mcp/__init__.py`**
Content: (empty string)

**File 3: `{output_dir}/src/{service}_mcp/server.py`**
Start from this template and add one @mcp.tool() function per endpoint:
{server_template}

For each endpoint below, add an @mcp.tool() async function that:
- Takes typed parameters (use Pydantic BaseModel for complex inputs)
- Calls `_request(method, path, ...)` with the correct HTTP method and path
- Returns a formatted JSON string of the response
- Has a clear docstring

## Step 2: Install & validate

**STOP! Do NOT output your final answer yet.** After writing all files, you MUST call the terminal tool for each of these commands in order:

1. Install dependencies: call terminal with command `cd {output_dir} && uv sync`
   — The server WILL NOT work without this step.
2. Import check: call terminal with command `cd {output_dir} && .venv/bin/python -c "from {service}_mcp.server import mcp; print('Import OK')"`
   — If this fails, fix the code with str_replace and retry.
3. Server startup test: call terminal with command `cd {output_dir} && timeout 5 .venv/bin/python -m {service}_mcp.server 2>&1 || true`
   — A timeout exit means success.

**Do NOT output your final answer or config JSON until steps 1-2 above have been executed.**

## Endpoints to implement

{endpoints}

## Rules
- Call write_file directly as a tool call — never output code as chat text
- One @mcp.tool() per endpoint listed above
- Include `if __name__ == "__main__": mcp.run()` at the end of server.py
- No empty stubs or `pass` — every tool must call _request()
"""

_SLIM_INTERNAL = """\
# MCP BUILDER MODE

You are building a Python MCP server at `{output_dir}` for an internal product.
You have these native tools available: `write_file`, `read_file`, `str_replace`, `delete_file`, `terminal`, `knowledge_base`, `web_search`, `web_fetch`.
Call them directly as tool calls — do NOT write Python code blocks.

## Step 1: Understand the product's API

Call the knowledge_base tool for each of these queries to discover exact routes, parameters, and auth:

1. Call knowledge_base with action="search", query="route handler function signatures request body parameters", keywords=["def ", "async def", "@router", "@app", "BaseModel", "request", "body", "params"]

2. Call knowledge_base with action="search", query="router mounting app include_router prefix API versioning", keywords=["include_router", "prefix=", "/api/v", "app.mount", "APIRouter"]

3. Call knowledge_base with action="search", query="authentication middleware authorization header API key token", keywords=["Authorization", "Bearer", "X-API-Key", "api_key", "token", "authenticate"]

After these queries, call knowledge_base with action="read_file", file_path="<path from search results>" for the top 2-3 most relevant source files.

**CRITICAL — SDK Auth Code:** If the KB contains files under `sdk/` paths (from the official SDK),
you MUST read them and copy the EXACT authentication/signing implementation into your server code.
Do NOT reimplement auth from docs — use the proven SDK code. Search for it:
Call knowledge_base with action="search", query="signature authentication signing HMAC credentials", keywords=["sign", "hmac", "signature", "auth", "credential", "signer", "sdk"]

## Step 2: Create project files

Call write_file for each of these files:

**File 1: `{output_dir}/pyproject.toml`**
Content:
{pyproject}

**File 2: `{output_dir}/src/{service}_mcp/__init__.py`**
Content: (empty string)

**File 3: `{output_dir}/src/{service}_mcp/server.py`**
Start from this template:
{server_template}

For each endpoint you discovered in Step 1, add an @mcp.tool() async function.
Use the EXACT field names, route prefixes, and auth patterns from the KB.

## Step 3: Install & validate

**STOP! Do NOT output your final answer yet.** After writing all files, you MUST call the terminal tool for each of these commands in order:

1. Install dependencies: call terminal with command `cd {output_dir} && uv sync`
   — The server WILL NOT work without this step.
2. Import check: call terminal with command `cd {output_dir} && .venv/bin/python -c "from {service}_mcp.server import mcp; print('Import OK')"`
   — If this fails, fix the code with str_replace and retry.
3. Server startup test: call terminal with command `cd {output_dir} && timeout 5 .venv/bin/python -m {service}_mcp.server 2>&1 || true`
   — A timeout exit means success.

**Do NOT output your final answer or config JSON until steps 1-2 above have been executed.**

## Rules
- Call tools directly as native tool calls — never write Python code blocks
- One @mcp.tool() per endpoint
- Use the EXACT route prefix from Step 1 (e.g., /api/v1)
- Use the EXACT auth pattern from Step 1
- Include `if __name__ == "__main__": mcp.run()` at the end of server.py
- No empty stubs or `pass` — every tool must call _request()
"""


_DISCOVERY_AGENT_PROMPT = """\
# API DOCUMENTATION CRAWLER

Your ONLY job is to crawl API documentation and store every relevant page in the knowledge base.
Do NOT extract endpoints or return JSON. Just fetch pages and store them.

## Documentation URL: {api_docs_url}
## API Name: {api_name}

## How to store a page

After fetching any page, store it:
```python
result = web_fetch("<url>")
print(result[:2000])
knowledge_base(action="store_page", title="<short descriptive title>", text=result)
```

## Step 1: Fetch the main page

```python
result = web_fetch("{api_docs_url}")
print(result[:5000])
knowledge_base(action="store_page", title="{api_name} - Main Documentation", text=result)
```

Read the page. Find links to:
- API reference / method listings / endpoint documentation
- Authentication / authorization docs
- OpenAPI / Swagger spec files (.json, .yaml)
- GitHub SDK repos (for auth source code)

## Step 2: Fetch and store relevant pages

Fetch each relevant link and store it. Be STRATEGIC:

**FETCH these** (high value):
- Pages with endpoint details (GET /users, POST /orders, etc.)
- Authentication / signing documentation
- Individual method pages (e.g., /docs/api/chat.postMessage)
- OpenAPI/Swagger spec files
- GitHub SDK auth source files

**SKIP these** (low value):
- Blog posts, changelogs, pricing, support, FAQ
- Tutorials, quickstart guides (unless they show auth setup)
- Social media, login, dashboard links

**If you find an OpenAPI/Swagger spec URL** — fetch and store it immediately.
It contains all endpoints. You can stop crawling after that.

**If you find a GitHub SDK repo** — fetch these specific files:
- Any file with "auth", "sign", "hmac", "credential", "signature" in the name
- The main client file (client.py, api.py, etc.)
Store each as a separate page.

## Step 3: Confirm what you stored

When done, respond with a plain text summary:
- How many pages you stored
- What topics they cover (endpoints, auth, SDK, etc.)
- Any important links you found but couldn't fetch

That's it. Do NOT extract endpoints. Do NOT return JSON. Just crawl and store.
"""


_SLIM_EXTERNAL_WITH_KB = """\
# MCP BUILDER MODE

You are building a Python MCP server at `{output_dir}`.
You have access to a knowledge base containing the full API documentation.
You have these native tools available: `write_file`, `read_file`, `str_replace`, `delete_file`, `terminal`, `knowledge_base`, `web_search`, `web_fetch`.
Call them directly as tool calls — do NOT write Python code blocks.

## Step 1: Research the API (MANDATORY — do this BEFORE writing any code)

Call the knowledge_base tool for each of these queries to understand the API deeply:

1. Authentication: call knowledge_base with action="search", query="authentication signing headers API key secret token", keywords=["auth", "sign", "hmac", "header", "credential", "token", "key", "secret"]

2. Endpoint parameters: call knowledge_base with action="search", query="place order parameters request body fields", keywords=["order", "parameter", "field", "required", "body"]

3. SDK source code: call knowledge_base with action="search", query="SDK source code signature algorithm implementation", keywords=["sdk", "source", "sign", "hmac", "sha", "base64", "signature"]

4. Error handling: call knowledge_base with action="search", query="error codes response format status", keywords=["error", "response", "status", "code", "exception"]

IMPORTANT: Read the KB results carefully. Use the EXACT authentication method, header names,
signing algorithm, and parameter names from the documentation. Do NOT guess or use generic patterns.

## Step 2: Create project files

Call write_file for each of these files:

**File 1: `{output_dir}/pyproject.toml`**
Content:
{pyproject}

**File 2: `{output_dir}/src/{service}_mcp/__init__.py`**
Content: (empty string)

**File 3: `{output_dir}/src/{service}_mcp/server.py`**
Use this as a starting template, but REPLACE the auth code with what you found in the KB:
{server_template}

## Step 3: Install & validate

**STOP! Do NOT output your final answer yet.** After writing all files, you MUST call the terminal tool for each of these commands in order:

1. Install dependencies: call terminal with command `cd {output_dir} && rm -f uv.lock && uv sync`
   — The server WILL NOT work without this step.
2. Import check: call terminal with command `cd {output_dir} && .venv/bin/python -c "from {service}_mcp.server import mcp; print('Import OK')"`
   — If this fails, fix the code with str_replace and retry.
3. Server startup test: call terminal with command `cd {output_dir} && timeout 5 .venv/bin/python -m {service}_mcp.server 2>&1 || true`
   — A timeout exit means success.

**Do NOT output your final answer or config JSON until steps 1-2 above have been executed.**

## Step 4: Verify with a real API call (MANDATORY)

After installing, test the server by making a real HTTP request:
1. Call write_file to create `{output_dir}/test_auth.py` with a script that imports `_request` from the server and calls a simple read-only endpoint
2. Call terminal with command `cd {output_dir} && .venv/bin/python test_auth.py 2>&1`
3. If you get a 401/signature error, fix the auth code with str_replace and retest (up to 3 times)
4. Call terminal with command `rm -f {output_dir}/test_auth.py` to clean up

## Endpoints to implement
{endpoints}

## Rules
- Call tools directly as native tool calls — never write Python code blocks
- Query the KB FIRST, then write code based on what you found
- One @mcp.tool() per endpoint
- Use the EXACT auth implementation from the KB/SDK (not generic X-API-Key)
- If auth doesn't work, try importing the official SDK's signing function instead of reimplementing
- Include `if __name__ == "__main__": mcp.run()` at the end of server.py
- No empty stubs or `pass` — every tool must call _request()
"""


def build_mcp_builder_supplement_slim(
    output_dir: str,
    service_name: str,
    is_external: bool = True,
    has_kb: bool = False,
    api_docs_text: Optional[str] = None,
    api_base_url: Optional[str] = None,
    auth_type: Optional[str] = None,
    auth_details: Optional[str] = None,
    selected_endpoints: Optional[list] = None,
) -> str:
    """Build a slim (~6K char) MCP Builder prompt supplement.

    Replaces the 58K char full supplement with focused, non-conflicting instructions.
    Three modes:
    - External without KB: flat endpoint list + code template
    - External with KB: KB query instructions + endpoints + template
    - Internal: KB queries for source code + template
    """
    service = service_name.lower().replace(" ", "_").replace("-", "_")
    SERVICE = service.upper()

    # Auth code snippet — use detailed auth if available, else fall back to generic
    auth = auth_type or "bearer"
    if auth_details:
        auth_code = f'# Auth: {auth_details}\n    # Implement the authentication described above using env vars prefixed with {SERVICE}_'
    else:
        auth_code = _AUTH_SNIPPETS.get(auth, _AUTH_SNIPPETS["bearer"]).format(SERVICE=SERVICE)

    # Server template
    server_template = _SLIM_SERVER_TEMPLATE.format(
        service=service,
        base_url=api_base_url or "http://localhost:8000",
        auth_code=auth_code,
    )

    # Pyproject
    pyproject = _SLIM_PYPROJECT_TEMPLATE.format(service=service)

    # Endpoints list
    endpoints = ""
    if selected_endpoints:
        ep_lines = []
        for ep in selected_endpoints:
            line = f"- **{ep.get('method', 'GET')} {ep.get('path', '/')}** — {ep.get('description', '')}"
            line += f"\n  Tool name: `{ep.get('suggested_tool_name', 'unknown')}`"
            params = ep.get("parameters", [])
            if params:
                for p in params:
                    req = " (required)" if p.get("required") else ""
                    line += f"\n  - `{p.get('name')}`: {p.get('type', 'string')}{req} [{p.get('location', 'body')}]"
            ep_lines.append(line)
        endpoints = "\n\n".join(ep_lines)

    # External with KB — agent queries KB for auth, params, SDK code
    if is_external and has_kb:
        return _SLIM_EXTERNAL_WITH_KB.format(
            output_dir=output_dir,
            service=service,
            pyproject=pyproject,
            server_template=server_template,
            endpoints=endpoints,
        )

    # External without KB — flat endpoint list
    if is_external:
        # Inject API docs if provided
        if api_docs_text:
            endpoints += f"\n\n## API Documentation Reference\n\n{api_docs_text[:20000]}"
        return _SLIM_EXTERNAL.format(
            output_dir=output_dir,
            service=service,
            pyproject=pyproject,
            server_template=server_template,
            endpoints=endpoints,
        )

    # Internal — KB queries for source code
    return _SLIM_INTERNAL.format(
        output_dir=output_dir,
        service=service,
        pyproject=pyproject,
        server_template=server_template,
    )


def build_discovery_agent_prompt(
    api_name: str,
    api_docs_url: str,
) -> str:
    """Build the Phase 1 discovery agent prompt.

    The agent crawls docs, stores in KB, and discovers endpoints.
    """
    return _DISCOVERY_AGENT_PROMPT.format(
        api_name=api_name,
        api_docs_url=api_docs_url,
    )


def build_mcp_builder_supplement(
    language: str,
    output_dir: str,
    include_eval: bool = False,
    api_docs_url: Optional[str] = None,
    api_docs_text: Optional[str] = None,
    api_base_url: Optional[str] = None,
    auth_type: Optional[str] = None,
) -> str:
    """Build the full MCP Builder system prompt supplement.

    Args:
        language: "typescript" or "python"
        output_dir: Absolute path where the MCP project should be written.
        include_eval: If True, also append the evaluation guide.
        api_docs_url: External API documentation URL (triggers external mode).
        api_docs_text: Direct injected raw documentation or sliced API JSON.
        api_base_url: External API base URL.
        auth_type: Auth pattern: bearer, api_key, basic, or none.
    """
    lang_key = "typescript" if language == "typescript" else "python"
    lang_label = "TypeScript" if language == "typescript" else "Python"

    max_ref_chars = 30000
    best_practices = _read_ref("best_practices")
    language_guide = _read_ref(lang_key)

    # Truncate reference docs at section boundaries to fit context window
    def _truncate_ref(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        # Try to cut at a section break (---) near the limit
        cut = text[:limit].rfind("\n---")
        if cut > limit * 0.5:
            return text[:cut] + "\n\n[... reference truncated for context limits ...]"
        # Fall back to paragraph break
        cut = text[:limit].rfind("\n\n")
        if cut > limit * 0.5:
            return text[:cut] + "\n\n[... reference truncated for context limits ...]"
        return text[:limit] + "\n\n[... reference truncated for context limits ...]"

    best_practices = _truncate_ref(best_practices, max_ref_chars)
    language_guide = _truncate_ref(language_guide, max_ref_chars)

    supplement = _MCP_BUILDER_WORKFLOW.format(
        output_dir=output_dir,
        language=language,
        language_label=lang_label,
        best_practices=best_practices,
        language_guide=language_guide,
    )

    # Append external API instructions when building for an external service
    if api_docs_text:
        supplement += _EXTERNAL_API_BLOCK_TEXT.format(
            api_docs_text=api_docs_text,
            api_base_url=api_base_url or "Not specified",
            auth_type=auth_type or "none"
        )
    elif api_docs_url:
        supplement += _EXTERNAL_API_BLOCK_URL.format(
            api_docs_url=api_docs_url,
            api_base_url=api_base_url or "Not specified",
            auth_type=auth_type or "none"
        )

    if include_eval:
        eval_guide = _read_ref("evaluation")
        supplement += (
            "\n\n---\n\n## Reference: Evaluation Guide\n\n" + eval_guide
        )

    return supplement
