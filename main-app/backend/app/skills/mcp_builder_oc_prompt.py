"""
MCP Builder prompt — OpenCode engine.

The OpenCode port of `mcp_builder_prompt.build_mcp_builder_supplement_slim`.
Same three build modes (internal / external / external-with-KB) and the same
code templates, but the instructions reference OpenCode's native tools instead
of the legacy LangGraph CodeAct tools:

    write_file(path, content)     -> write       (OC native)
    read_file(path)               -> read        (OC native)
    str_replace(path, old, new)   -> edit        (OC native)
    delete_file(path)             -> bash `rm`   (OC native)
    terminal(command)             -> bash        (OC native)
    knowledge_base(action="...")  -> retrace-kb_search / retrace-kb_read_file
                                                 (retrace-kb MCP)

`MCP_BUILDER_OC_PROMPT` is the short, static system prompt for the `mcp-builder`
OC agent (set in opencode_manager.generate_config). The per-build instructions —
endpoints, output dir, code templates — are produced by
`build_mcp_builder_oc_message()` and sent as the session message.
"""

from typing import Optional

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Static agent prompt — set as the `mcp-builder` OC agent's `prompt` field.
# Short and engine-level; the actual per-build spec arrives in the message.
# ---------------------------------------------------------------------------

MCP_BUILDER_OC_PROMPT = """\
You are ReTrace MCP Builder — an autonomous coding agent that scaffolds,
implements, and validates complete MCP (Model Context Protocol) servers.

You run in an automated background pipeline. There is no human to confirm
with — the user already approved the build in the UI. Execute the build
instructions in the user message to completion without pausing for approval.

## Tools

- `write` — create a file. Pass the absolute path and the full file content.
- `read` — read a file before editing it.
- `edit` — exact-string replacement in a file (use for targeted fixes).
- `bash` — run shell commands (`mkdir`, `uv sync`, `py_compile`, import checks).
- `retrace-kb_search` / `retrace-kb_read_file` — query the team knowledge base.
- `webfetch` — fetch a URL when external API docs need to be read.

## Hard rules

- NEVER paste source code into your chat reply for the user to copy. ALL code
  goes to disk via the `write` tool. Outputting code as text is a failure.
- Do NOT start the MCP server — stdio servers hang waiting for input. The
  import check in the build instructions is sufficient validation.
- Run every validation step in the build instructions, in order. Fix errors
  and re-run before moving on.
- When the build is complete, your final reply MUST contain the MCP client
  config as a fenced ```json block and a Quick Start ```bash block, exactly
  as the build instructions specify — the UI parses these.
"""


# ---------------------------------------------------------------------------
# Code templates — identical to the legacy slim prompt (no tool names here,
# this is generated server code).
# ---------------------------------------------------------------------------

_SERVER_TEMPLATE = '''import os
import json
from typing import Optional
from enum import Enum
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("{service}_mcp")

API_BASE_URL = "{base_url}"


class ResponseFormat(Enum):
    """Output format for tool responses."""
    MARKDOWN = "markdown"
    JSON = "json"


def _get_headers() -> dict:
    """Build auth headers."""
    headers = {{"Content-Type": "application/json", "Accept": "application/json"}}
    {auth_code}
    return headers


def _handle_api_error(status_code: int, response_text: str) -> str:
    """Map HTTP status codes to actionable error messages."""
    error_map = {{
        400: "Bad request — check your parameters",
        401: "Authentication failed — verify your API token",
        403: "Forbidden — insufficient permissions for this resource",
        404: "Resource not found — check the ID or path",
        409: "Conflict — resource already exists or state conflict",
        422: "Validation error — check parameter types and values",
        429: "Rate limited — wait and retry in a few seconds",
        500: "Internal server error — the API is experiencing issues",
        502: "Bad gateway — the API is temporarily unavailable",
        503: "Service unavailable — the API is under maintenance",
    }}
    msg = error_map.get(status_code, f"HTTP {{status_code}} error")
    detail = response_text[:500] if response_text else ""
    return f"Error: {{msg}}. Details: {{detail}}"


async def _request(method: str, path: str, params: dict = None, json_body: dict = None) -> dict:
    """Shared HTTP client with error handling."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.request(method, f"{{API_BASE_URL}}{{path}}", params=params, json=json_body, headers=_get_headers())
        if resp.status_code >= 400:
            return {{"error": _handle_api_error(resp.status_code, resp.text)}}
        return resp.json() if resp.content else {{"status": "ok"}}


async def _paginated_request(method: str, path: str, params: dict = None, limit: int = 50, offset: int = 0) -> dict:
    """Fetch a paginated list endpoint. Returns items + pagination metadata."""
    params = params or {{}}
    params["limit"] = limit
    params["offset"] = offset
    result = await _request(method, path, params=params)
    if "error" in result:
        return result
    if isinstance(result, list):
        return {{"items": result, "has_more": len(result) >= limit, "next_offset": offset + len(result)}}
    return result


def _format_response(data: dict, fmt: ResponseFormat = ResponseFormat.MARKDOWN) -> str:
    """Format response as markdown or JSON."""
    if fmt == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)
    if "error" in data:
        return f"**Error:** {{data[\'error\']}}"
    lines = []
    for key, value in data.items():
        if isinstance(value, list):
            lines.append(f"**{{key}}:** {{len(value)}} items")
            for item in value[:10]:
                if isinstance(item, dict):
                    summary = ", ".join(f"{{k}}={{v}}" for k, v in list(item.items())[:4])
                    lines.append(f"  - {{summary}}")
                else:
                    lines.append(f"  - {{item}}")
            if len(value) > 10:
                lines.append(f"  - ... and {{len(value) - 10}} more")
        else:
            lines.append(f"**{{key}}:** {{value}}")
    return "\\n".join(lines)


# --- Add @mcp.tool() functions below, one per endpoint ---
# Use annotations: @mcp.tool(annotations={{"readOnlyHint": True}}) for GET endpoints
# Use annotations: @mcp.tool(annotations={{"destructiveHint": True}}) for DELETE endpoints
# Use annotations: @mcp.tool(annotations={{"idempotentHint": True}}) for PUT endpoints
# For list endpoints, use _paginated_request() instead of _request()
# Return _format_response(result) for consistent output
'''

_PYPROJECT_TEMPLATE = '''[build-system]
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


# ---------------------------------------------------------------------------
# Per-build instruction templates — OpenCode tool names.
# ---------------------------------------------------------------------------

_EXTERNAL = """\
# MCP BUILD TASK

Build a Python MCP server at `{output_dir}`.
Use the `write` tool to create files, `bash` to run commands.

## Step 1: Create project files

Use `write` to create each of these files (absolute paths):

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

After writing all files, run these `bash` commands in order:

1. `cd {output_dir} && uv sync` — the server will not work without this.
2. `cd {output_dir} && .venv/bin/python -c "from {service}_mcp.server import mcp; print('Import OK')"`
   — if this fails, fix the code with `edit` and retry.
3. Do NOT run the server (stdio servers hang). The import check is sufficient.

## Endpoints to implement

{endpoints}

## Rules
- Use the `write` tool for files — never output code as chat text.
- One @mcp.tool() per endpoint listed above.
- Include `if __name__ == "__main__": mcp.run()` at the end of server.py.
- No empty stubs or `pass` — every tool must call _request().
- `@mcp.tool(annotations={{"readOnlyHint": True}})` for GET endpoints.
- `@mcp.tool(annotations={{"destructiveHint": True}})` for DELETE endpoints.
- `@mcp.tool(annotations={{"idempotentHint": True}})` for PUT endpoints.
- For list endpoints that may return many items, use `_paginated_request()`.
- Return `_format_response(result)` for consistent output.
- Error handling is built into `_request()` — do not add try/except around it.
"""

_INTERNAL = """\
# MCP BUILD TASK

Build a Python MCP server at `{output_dir}` for an internal team.
Use `retrace-kb_search` / `retrace-kb_read_file` to research the team's API,
the `write` tool to create files, `bash` to run commands.

## Step 1: Understand the team's API

Call `retrace-kb_search` for each of these queries to discover exact routes,
parameters, and auth:

1. `retrace-kb_search(query="route handler function signatures request body parameters", keywords="def ,async def,@router,@app,BaseModel,request,body,params")`

2. `retrace-kb_search(query="router mounting app include_router prefix API versioning", keywords="include_router,prefix=,/api/v,app.mount,APIRouter")`

3. `retrace-kb_search(query="authentication middleware authorization header API key token", keywords="Authorization,Bearer,X-API-Key,api_key,token,authenticate")`

Then call `retrace-kb_read_file(file_path="<path from search results>")` for the
top 2-3 most relevant source files.

**CRITICAL — SDK Auth Code:** If the KB has files under `sdk/` paths, read them
and copy the EXACT auth/signing implementation into your server code. Do NOT
reimplement auth from docs. Search for it:
`retrace-kb_search(query="signature authentication signing HMAC credentials", keywords="sign,hmac,signature,auth,credential,signer,sdk")`

## Step 2: Create project files

Use `write` to create each of these files:

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

After writing all files, run these `bash` commands in order:

1. `cd {output_dir} && uv sync` — the server will not work without this.
2. `cd {output_dir} && .venv/bin/python -c "from {service}_mcp.server import mcp; print('Import OK')"`
   — if this fails, fix the code with `edit` and retry.
3. Do NOT run the server (stdio servers hang). The import check is sufficient.

## Rules
- Use the `write` tool for files — never output code as chat text.
- One @mcp.tool() per endpoint.
- Use the EXACT route prefix and auth pattern from Step 1.
- Include `if __name__ == "__main__": mcp.run()` at the end of server.py.
- No empty stubs or `pass` — every tool must call _request().
- `@mcp.tool(annotations={{"readOnlyHint": True}})` for GET endpoints.
- `@mcp.tool(annotations={{"destructiveHint": True}})` for DELETE endpoints.
- `@mcp.tool(annotations={{"idempotentHint": True}})` for PUT endpoints.
- For list endpoints that may return many items, use `_paginated_request()`.
- Return `_format_response(result)` for consistent output.
- Error handling is built into `_request()` — do not add try/except around it.
"""

_EXTERNAL_WITH_KB = """\
# MCP BUILD TASK

Build a Python MCP server at `{output_dir}`.
A knowledge base contains the full API documentation. Use
`retrace-kb_search` / `retrace-kb_read_file` to research it, the `write` tool
to create files, `bash` to run commands.

## Step 1: Research the API (MANDATORY — before writing any code)

Call `retrace-kb_search` for each of these queries:

1. `retrace-kb_search(query="authentication signing headers API key secret token", keywords="auth,sign,hmac,header,credential,token,key,secret")`

2. `retrace-kb_search(query="endpoint parameters request body fields required", keywords="parameter,field,required,body,request")`

3. `retrace-kb_search(query="SDK source code signature algorithm implementation", keywords="sdk,source,sign,hmac,sha,base64,signature")`

4. `retrace-kb_search(query="error codes response format status", keywords="error,response,status,code,exception")`

Use the EXACT auth method, header names, signing algorithm, and parameter names
from the KB. Do NOT guess or use generic patterns.

## Step 2: Create project files

Use `write` to create each of these files:

**File 1: `{output_dir}/pyproject.toml`**
Content:
{pyproject}

**File 2: `{output_dir}/src/{service}_mcp/__init__.py`**
Content: (empty string)

**File 3: `{output_dir}/src/{service}_mcp/server.py`**
Use this as a starting template, but REPLACE the auth code with what you
found in the KB:
{server_template}

## Step 3: Install & validate

After writing all files, run these `bash` commands in order:

1. `cd {output_dir} && rm -f uv.lock && uv sync` — the server will not work without this.
2. `cd {output_dir} && .venv/bin/python -c "from {service}_mcp.server import mcp; print('Import OK')"`
   — if this fails, fix the code with `edit` and retry.
3. Do NOT run the server (stdio servers hang). The import check is sufficient.

## Endpoints to implement
{endpoints}

## Rules
- Use the `write` tool for files — never output code as chat text.
- Research the KB FIRST, then write code based on what you found.
- One @mcp.tool() per endpoint.
- Use the EXACT auth implementation from the KB/SDK (not generic X-API-Key).
- Include `if __name__ == "__main__": mcp.run()` at the end of server.py.
- No empty stubs or `pass` — every tool must call _request().
- `@mcp.tool(annotations={{"readOnlyHint": True}})` for GET endpoints.
- `@mcp.tool(annotations={{"destructiveHint": True}})` for DELETE endpoints.
- `@mcp.tool(annotations={{"idempotentHint": True}})` for PUT endpoints.
- For list endpoints that may return many items, use `_paginated_request()`.
- Return `_format_response(result)` for consistent output.
- Error handling is built into `_request()` — do not add try/except around it.
"""

_DELIVER_BLOCK = """\

## Final delivery (after validation passes)

Your final reply MUST contain:

1. A ready-to-paste MCP client config as a fenced ```json block:
   ```json
   {{
     "mcpServers": {{
       "{service}": {{
         "command": "{output_dir}/.venv/bin/python",
         "args": ["-m", "{service}_mcp.server"],
         "env": {{ "{SERVICE}_API_TOKEN": "<your-token>" }}
       }}
     }}
   }}
   ```
   The `command` MUST be the absolute path to `.venv/bin/python` created by
   `uv sync`. The env var name must match the one used in the server code.

2. A "Quick Start" section as a fenced ```bash block:
   ```bash
   cd {output_dir}
   uv sync
   {output_dir}/.venv/bin/python -m {service}_mcp.server
   ```
"""


def build_mcp_builder_oc_message(
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
    """Build the per-build session message for the `mcp-builder` OC agent.

    OpenCode port of `mcp_builder_prompt.build_mcp_builder_supplement_slim` —
    same three modes, same code templates, OpenCode tool names. The returned
    string is sent as the session message; the static guidance lives in
    `MCP_BUILDER_OC_PROMPT` (the agent's `prompt`).
    """
    service = service_name.lower().replace(" ", "_").replace("-", "_")
    SERVICE = service.upper()

    # Auth code snippet for the server template.
    auth = auth_type or "bearer"
    if auth_details:
        auth_code = (
            f"# Auth: {auth_details}\n"
            f"    # Implement the authentication described above using env vars "
            f"prefixed with {SERVICE}_"
        )
    else:
        auth_code = _AUTH_SNIPPETS.get(auth, _AUTH_SNIPPETS["bearer"]).format(SERVICE=SERVICE)

    server_template = _SERVER_TEMPLATE.format(
        service=service,
        base_url=api_base_url or "http://localhost:8000",
        auth_code=auth_code,
    )
    pyproject = _PYPROJECT_TEMPLATE.format(service=service)

    # Endpoint list.
    endpoints = ""
    if selected_endpoints:
        ep_lines = []
        for ep in selected_endpoints:
            line = f"- **{ep.get('method', 'GET')} {ep.get('path', '/')}** — {ep.get('description', '')}"
            line += f"\n  Tool name: `{ep.get('suggested_tool_name', 'unknown')}`"
            for p in ep.get("parameters", []) or []:
                req = " (required)" if p.get("required") else ""
                line += f"\n  - `{p.get('name')}`: {p.get('type', 'string')}{req} [{p.get('location', 'body')}]"
            ep_lines.append(line)
        endpoints = "\n\n".join(ep_lines)

    deliver = _DELIVER_BLOCK.format(output_dir=output_dir, service=service, SERVICE=SERVICE)

    if is_external and has_kb:
        body = _EXTERNAL_WITH_KB.format(
            output_dir=output_dir, service=service,
            pyproject=pyproject, server_template=server_template, endpoints=endpoints,
        )
    elif is_external:
        if api_docs_text:
            endpoints += f"\n\n## API Documentation Reference\n\n{api_docs_text[:20000]}"
        body = _EXTERNAL.format(
            output_dir=output_dir, service=service,
            pyproject=pyproject, server_template=server_template, endpoints=endpoints,
        )
    else:
        body = _INTERNAL.format(
            output_dir=output_dir, service=service,
            pyproject=pyproject, server_template=server_template,
        )

    return body + deliver
