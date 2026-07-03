import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional, Any
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog
import asyncio

from app.db.database import get_session, async_session_maker
# MCP Builder runs entirely on OpenCode (the `mcp-builder` OC agent). The
# legacy LangGraph CodeAct path (AgentService) was removed in ADR-002 Phase 5.
from app.core.security import CurrentUser, get_current_user
from app.api.settings import get_active_llm_settings
from app.services.mcp_builder_service import mcp_builder_service

logger = structlog.get_logger()
router = APIRouter()

# ---------------------------------------------------------------------------
# Document Upload & Text Extraction
# ---------------------------------------------------------------------------

def _extract_pdf_smart(path: str) -> tuple[str, str]:
    """Per-page smart PDF extraction.

    - Pages with NO images → pymupdf text extraction (fast, accurate)
    - Pages WITH images → OCR the whole page (catches all visible text)

    Returns (extracted_text, method) where method is "text", "ocr", or "hybrid".
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        raise HTTPException(status_code=500, detail="pymupdf not installed. Cannot process PDFs.")

    doc = fitz.open(path)
    pages_text: list[str] = []
    ocr_page_count = 0
    text_page_count = 0

    try:
        for page_num, page in enumerate(doc):
            images = page.get_images(full=True)

            if not images:
                # No images on this page → fast pymupdf text extraction
                text = page.get_text() or ""
                if text.strip():
                    pages_text.append(text.strip())
                    text_page_count += 1
            else:
                # Page has images → OCR the whole page to catch everything
                ocr_text = _ocr_page(page)
                if ocr_text and ocr_text.strip():
                    pages_text.append(ocr_text.strip())
                    ocr_page_count += 1
                else:
                    # OCR failed/unavailable — fall back to pymupdf text
                    text = page.get_text() or ""
                    if text.strip():
                        pages_text.append(text.strip())
                        text_page_count += 1
    finally:
        doc.close()

    combined = "\n\n".join(pages_text)
    total = text_page_count + ocr_page_count

    if ocr_page_count == 0:
        method = "text"
    elif text_page_count == 0:
        method = "ocr"
    else:
        method = "hybrid"

    logger.info("pdf_smart_extract", pages=total, text_pages=text_page_count, ocr_pages=ocr_page_count, chars=len(combined), method=method)
    return combined, method


def _ocr_page(page) -> Optional[str]:
    """Render a pymupdf page to image and OCR it with Tesseract."""
    try:
        import pytesseract
        from PIL import Image
        import io
    except ImportError:
        logger.warning("ocr_deps_missing", hint="Install pytesseract and Pillow for OCR support")
        return None

    try:
        # Check if Tesseract is available
        pytesseract.get_tesseract_version()
    except Exception:
        logger.warning("tesseract_not_installed", hint="brew install tesseract")
        return None

    try:
        # Render page to image at 200 DPI (good balance of speed vs quality)
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_bytes))

        # Run OCR
        text = pytesseract.image_to_string(img)
        return text
    except Exception as exc:
        logger.warning("ocr_page_failed", error=str(exc))
        return None


def _extract_docx(path: str) -> str:
    """Extract text from DOCX including paragraphs and tables."""
    try:
        from docx import Document as DocxDocument
    except ImportError:
        raise HTTPException(status_code=500, detail="python-docx not installed. Cannot process DOCX files.")

    try:
        doc = DocxDocument(path)
        parts: list[str] = []

        # Extract paragraphs
        for p in doc.paragraphs:
            if p.text.strip():
                parts.append(p.text.strip())

        # Extract tables (API docs often use tables for parameters)
        for table in doc.tables:
            table_rows: list[str] = []
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    table_rows.append(row_text)
            if table_rows:
                parts.append("\n".join(table_rows))

        return "\n\n".join(parts)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read DOCX file: {str(exc)}")


@router.post("/upload-docs")
async def upload_documentation(file: UploadFile = File(...), current_user: CurrentUser = Depends(get_current_user)):
    """Upload a documentation file (PDF, DOCX, TXT, JSON, YAML) and extract text."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    ext = os.path.splitext(file.filename)[1].lower()
    allowed = {".pdf", ".docx", ".doc", ".txt", ".md", ".json", ".yaml", ".yml"}

    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}. Allowed: {', '.join(sorted(allowed))}")

    logger.info("mcp_builder_upload_start", filename=file.filename, ext=ext)

    # Save to temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        pages = 0
        method = "text"

        if ext == ".pdf":
            text, method = _extract_pdf_smart(tmp_path)
            # Count pages
            try:
                import fitz
                doc = fitz.open(tmp_path)
                pages = len(doc)
                doc.close()
            except Exception:
                pass

        elif ext in (".docx", ".doc"):
            text = _extract_docx(tmp_path)
            pages = 1  # DOCX doesn't have meaningful page count for our purposes

        else:
            # Text files — read as UTF-8
            try:
                with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Failed to read file: {str(exc)}")

        if not text or not text.strip():
            raise HTTPException(
                status_code=400,
                detail="No text could be extracted from this file. For scanned PDFs, ensure Tesseract is installed (brew install tesseract)."
            )

        logger.info("mcp_builder_upload_complete", filename=file.filename, chars=len(text), pages=pages, method=method)

        return {
            "text": text,
            "pages": pages,
            "method": method,
            "filename": file.filename,
        }

    finally:
        # Clean up temp file
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# MCP Server Registry
# ---------------------------------------------------------------------------

def _check_server_status(destination_folder: str, module_name: str) -> str:
    """Check if an MCP server is running, stopped, or has errors."""
    import subprocess

    server_path = os.path.expanduser(f"{destination_folder}/src/{module_name}/server.py")

    # Check if files exist
    if not os.path.exists(server_path):
        return "error"

    # Check if process is running
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"{module_name}.server"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return "running"
    except Exception:
        pass

    return "stopped"


@router.get("/servers")
async def list_mcp_servers(session: AsyncSession = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    """List all built MCP servers with live status."""
    from app.models.mcp_server import McpServer
    from sqlalchemy import select

    result = await session.execute(
        select(McpServer).order_by(McpServer.created_at.desc())
    )
    servers = result.scalars().all()

    server_list = []
    for srv in servers:
        data = srv.to_dict()
        data["status"] = _check_server_status(srv.destination_folder, srv.module_name)
        server_list.append(data)

    return server_list


@router.get("/servers/{server_id}")
async def get_mcp_server(server_id: str, session: AsyncSession = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    """Get a single MCP server's details with live status."""
    from app.models.mcp_server import McpServer
    from sqlalchemy import select

    result = await session.execute(
        select(McpServer).where(McpServer.server_id == server_id)
    )
    srv = result.scalar_one_or_none()
    if not srv:
        raise HTTPException(status_code=404, detail="Server not found")

    data = srv.to_dict()
    data["status"] = _check_server_status(srv.destination_folder, srv.module_name)
    return data


@router.get("/progress/{server_name}")
async def get_generation_progress(
    server_name: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get the current generation progress for an MCP server build."""
    from app.services.mcp_builder_progress import get_progress

    progress = get_progress(server_name)
    if progress is None:
        return {"status": "not_found"}
    return {
        "status": progress.get("status", "in_progress"),
        "phase": progress.get("phase", ""),
        "iteration": progress.get("iteration", 0),
        "logs": progress.get("logs", [])[-10:],
        "result": progress.get("result"),
    }


@router.delete("/servers/{server_id}")
async def delete_mcp_server(
    server_id: str,
    delete_files: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Remove an MCP server from the registry. Optionally delete generated files on disk."""
    import shutil
    from app.models.mcp_server import McpServer
    from sqlalchemy import select

    result = await session.execute(
        select(McpServer).where(McpServer.server_id == server_id)
    )
    srv = result.scalar_one_or_none()
    if not srv:
        raise HTTPException(status_code=404, detail="Server not found")

    files_deleted = False
    if delete_files and srv.destination_folder:
        folder = srv.destination_folder
        if folder.startswith("~/") or folder.startswith("~\\"):
            folder = str(Path.home() / folder[2:])
        folder_path = Path(folder).resolve()

        # Safety: only delete if the folder's parent is named "mcp-servers"
        # e.g. ~/mcp-servers/gta ✓, ~/Desktop/mcp-servers/gta ✓
        # e.g. ~/Desktop ✗, ~/mcp-servers ✗, / ✗
        is_safe = (folder_path.parent.name == "mcp-servers"
                   and len(folder_path.parts) >= 3
                   and folder_path.name != "mcp-servers")

        if is_safe and folder_path.is_dir():
            shutil.rmtree(folder_path, ignore_errors=True)
            files_deleted = True
        elif not is_safe and folder_path.is_dir():
            logger.warning("delete_mcp_server_unsafe_path", path=str(folder_path),
                           reason="Path parent is not 'mcp-servers/', refusing to delete")

    # Also clean up linked MCP extension and all user installs
    extensions_deleted = 0
    user_installs_deleted = 0
    try:
        from app.models.mcp_extension import McpExtension
        from app.models.user_extension import UserExtension
        from app.services.mcp_client_service import mcp_client_manager

        ext_result = await session.execute(
            select(McpExtension).where(McpExtension.mcp_server_id == server_id)
        )
        linked_extensions = ext_result.scalars().all()

        for ext in linked_extensions:
            # Stop any running MCP connections for this extension
            if mcp_client_manager.is_running(ext.extension_id):
                await mcp_client_manager.stop_extension(ext.extension_id)

            # Delete all user installs for this extension
            ue_result = await session.execute(
                select(UserExtension).where(UserExtension.extension_id == ext.extension_id)
            )
            for ue in ue_result.scalars().all():
                await session.delete(ue)
                user_installs_deleted += 1

            await session.delete(ext)
            extensions_deleted += 1

        if extensions_deleted:
            logger.info("mcp_server_delete_cascade", server_id=server_id,
                        extensions=extensions_deleted, user_installs=user_installs_deleted)
    except Exception as cascade_err:
        logger.warning("mcp_server_delete_cascade_failed", error=str(cascade_err))

    await session.delete(srv)
    return {
        "status": "deleted",
        "server_id": server_id,
        "files_deleted": files_deleted,
        "extensions_deleted": extensions_deleted,
        "user_installs_deleted": user_installs_deleted,
    }


# ---------------------------------------------------------------------------
# MCP Server Start / Stop
# ---------------------------------------------------------------------------

_running_servers: dict[str, "subprocess.Popen[bytes]"] = {}


def _expand_dest(destination_folder: str) -> str:
    """Expand ~ in destination folder paths."""
    if destination_folder.startswith("~/") or destination_folder.startswith("~\\"):
        return str(Path.home() / destination_folder[2:])
    return os.path.expanduser(destination_folder)


@router.post("/servers/{server_id}/start")
async def start_mcp_server(server_id: str, session: AsyncSession = Depends(get_session)):
    """Start an MCP server process."""
    import subprocess
    from app.models.mcp_server import McpServer
    from sqlalchemy import select

    result = await session.execute(
        select(McpServer).where(McpServer.server_id == server_id)
    )
    srv = result.scalar_one_or_none()
    if not srv:
        raise HTTPException(status_code=404, detail="Server not found")

    dest = _expand_dest(srv.destination_folder)
    module_name = srv.module_name

    # Check if already running
    if _check_server_status(srv.destination_folder, module_name) == "running":
        return {"status": "already_running", "server_id": server_id}

    venv_python = os.path.join(dest, ".venv", "bin", "python")
    if os.path.isfile(venv_python):
        cmd = [venv_python, "-m", f"{module_name}.server"]
    else:
        # No venv yet — use uv run which auto-creates one
        cmd = ["uv", "run", "--directory", dest, "python", "-m", f"{module_name}.server"]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=dest,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _running_servers[server_id] = proc
        return {"status": "started", "server_id": server_id, "pid": proc.pid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start server: {e}")


@router.post("/servers/{server_id}/stop")
async def stop_mcp_server(server_id: str, session: AsyncSession = Depends(get_session)):
    """Stop a running MCP server process."""
    import subprocess
    import signal
    from app.models.mcp_server import McpServer
    from sqlalchemy import select

    result = await session.execute(
        select(McpServer).where(McpServer.server_id == server_id)
    )
    srv = result.scalar_one_or_none()
    if not srv:
        raise HTTPException(status_code=404, detail="Server not found")

    killed = False

    # Try killing from tracked processes first
    proc = _running_servers.pop(server_id, None)
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
            killed = True
        except Exception:
            try:
                proc.kill()
                killed = True
            except Exception:
                pass

    # Fallback: kill by pattern
    if not killed:
        module_name = srv.module_name
        try:
            subprocess.run(
                ["pkill", "-f", f"{module_name}.server"],
                capture_output=True, timeout=5,
            )
            killed = True
        except Exception:
            pass

    return {"status": "stopped", "server_id": server_id, "killed": killed}


class MCPDiscoverRequest(BaseModel):
    team_id: str

class MCPDiscoverExternalRequest(BaseModel):
    api_name: str
    api_docs_url: Optional[str] = None
    api_docs_text: Optional[str] = None

class MCPGeneratorRequest(BaseModel):
    team_id: Optional[str] = None # Native team ID or None for external
    team_name: str # Used to derive module_name and the build message
    destination_folder: str
    selected_endpoints: List[dict] # Full list of selected endpoints with schema
    api_docs_url: Optional[str] = None
    api_docs_text: Optional[str] = None # Added api_docs_text
    api_base_url: Optional[str] = None
    auth_type: Optional[str] = None
    auth_details: Optional[str] = None
    kb_team_id: Optional[str] = None

@router.post("/discover")
async def discover_mcp_endpoints(
    request: MCPDiscoverRequest,
    session: AsyncSession = Depends(get_session)
):
    logger.info("mcp_builder_discover_start", team_id=request.team_id)
    
    llm_settings = await get_active_llm_settings(session)
    api_key = llm_settings.get("api_key")
    if not api_key:
        raise HTTPException(status_code=400, detail="LLM API key is required to analyze the knowledge base.")
        
    model = llm_settings.get("model_name", "gpt-4o")
    api_url = llm_settings.get("api_url")
    
    provider = llm_settings.get("provider", "openai")
    endpoints = await mcp_builder_service.discover_endpoints(
        team_id=request.team_id,
        api_key=api_key,
        model=model,
        api_url=api_url,
        provider=provider,
    )
    
    return {"status": "success", "endpoints": [ep.model_dump() for ep in endpoints]}

@router.post("/discover-external")
async def discover_external_endpoints(
    request: MCPDiscoverExternalRequest,
    session: AsyncSession = Depends(get_session)
):
    """Discover endpoints from an external API docs URL."""
    logger.info("mcp_builder_discover_external_start", api_name=request.api_name, url=request.api_docs_url)

    llm_settings = await get_active_llm_settings(session)
    api_key = llm_settings.get("api_key")
    if not api_key:
        raise HTTPException(status_code=400, detail="LLM API key is required to analyze the API documentation.")

    model = llm_settings.get("model_name", "gpt-4o")
    api_url = llm_settings.get("api_url")
    provider = llm_settings.get("provider", "openai")

    if not request.api_docs_url and not request.api_docs_text:
        raise HTTPException(status_code=400, detail="Must provide either api_docs_url or api_docs_text")

    if request.api_docs_text:
        result = await mcp_builder_service.discover_from_text(
            api_name=request.api_name,
            api_docs_text=request.api_docs_text,
            api_key=api_key,
            model=model,
            api_url=api_url,
            provider=provider,
        )
    else:
        result = await mcp_builder_service.discover_from_url(
            api_name=request.api_name,
            api_docs_url=request.api_docs_url,
            api_key=api_key,
            model=model,
            api_url=api_url,
            provider=provider,
        )

    # Determine kb_team_id from the result
    safe_name = re.sub(r'[^a-z0-9]', '_', request.api_name.lower()).strip('_')
    from app.services.mcp_builder_service import MCP_TEAM_PREFIX
    kb_team_id = f"{MCP_TEAM_PREFIX}{safe_name}" if result.raw_pages else None

    return {
        "status": "success",
        "base_url": result.base_url,
        "auth_type": result.auth_type,
        "auth_details": result.auth_details,
        "endpoints": [ep.model_dump() for ep in result.endpoints],
        "pages_crawled": result.pages_crawled,
        "kb_team_id": kb_team_id,
    }

class MCPDiscoverAgentRequest(BaseModel):
    api_name: str
    api_docs_url: Optional[str] = None
    api_docs_text: Optional[str] = None


@router.post("/discover-agent")
async def discover_agent_endpoints(
    request: MCPDiscoverAgentRequest,
    session: AsyncSession = Depends(get_session)
):
    """Fast crawl + KB storage: backend crawler fetches docs, stores in KB, returns kb_team_id."""
    logger.info("mcp_builder_fetch_docs_start", api_name=request.api_name, url=request.api_docs_url)

    if not request.api_docs_url and not request.api_docs_text:
        raise HTTPException(status_code=400, detail="Must provide either api_docs_url or api_docs_text")

    safe_name = re.sub(r'[^a-z0-9]', '_', request.api_name.lower()).strip('_')
    from app.services.mcp_builder_service import store_mcp_docs_kb, _crawl_api_docs, _extract_text_from_html

    pages: list[tuple[str, str]] = []

    if request.api_docs_text:
        # Text/PDF upload — already have the content
        pages.append(("Uploaded API Documentation", request.api_docs_text))
    elif request.api_docs_url:
        # URL — fast backend crawl
        import httpx as _httpx

        # Fetch initial page
        async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
            resp = await http.get(request.api_docs_url, headers={
                "User-Agent": "ReTrace-MCP-Builder/1.0",
                "Accept": "text/html, application/json, */*",
            })
            resp.raise_for_status()
            initial_html = resp.text
            content_type = resp.headers.get("content-type", "")

        # Check if it's an OpenAPI spec
        if "json" in content_type or request.api_docs_url.endswith((".json", ".yaml", ".yml")):
            pages.append((f"{request.api_name} - OpenAPI Spec", initial_html))
        elif "html" in content_type:
            # Check for embedded OpenAPI spec link
            import re as _re
            spec_match = _re.search(r'href="([^"]*(?:openapi|swagger)[^"]*\.(?:json|yaml|yml))"', initial_html, _re.IGNORECASE)
            if spec_match:
                spec_url = spec_match.group(1)
                if not spec_url.startswith("http"):
                    from urllib.parse import urljoin
                    spec_url = urljoin(request.api_docs_url, spec_url)
                try:
                    async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
                        spec_resp = await http.get(spec_url)
                        spec_resp.raise_for_status()
                        pages.append((f"{request.api_name} - OpenAPI Spec", spec_resp.text))
                except Exception:
                    pass

            # Crawl depth-2
            crawled_text, pages_crawled, crawled_pages = await _crawl_api_docs(
                base_url=request.api_docs_url,
                initial_html=initial_html,
                max_pages=50,
                max_depth=2,
            )
            # Add the initial page
            pages.append((f"{request.api_name} - Main Documentation", _extract_text_from_html(initial_html)))
            # Add all crawled pages
            for url, text in crawled_pages:
                # Use the URL path as a title
                from urllib.parse import urlparse
                path = urlparse(url).path.rstrip("/").split("/")[-1] or "index"
                pages.append((f"{request.api_name} - {path}", text))

            logger.info("mcp_fetch_docs_crawled", pages=len(pages), total_chars=sum(len(t) for _, t in pages))

            # Also search for OpenAPI spec on GitHub if we didn't find one
            if not any("OpenAPI" in title for title, _ in pages):
                try:
                    from app.services.mcp_builder_service import MCPBuilderService
                    svc = MCPBuilderService()
                    llm_settings = await get_active_llm_settings(session)
                    spec_url = await svc._search_openapi_spec(request.api_name, llm_settings)
                    if spec_url:
                        async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
                            spec_resp = await http.get(spec_url)
                            spec_resp.raise_for_status()
                            pages.append((f"{request.api_name} - OpenAPI Spec (GitHub)", spec_resp.text))
                            logger.info("mcp_fetch_docs_openapi_found", url=spec_url)
                except Exception:
                    pass

            # Also search for SDK auth source code
            sdk_links = _re.findall(r'href="(https?://github\.com/[^"]+)"', initial_html)
            for sdk_url in sdk_links[:2]:  # Max 2 SDK repos
                try:
                    # Convert github.com URL to API URL to get file tree
                    parts = sdk_url.rstrip("/").split("/")
                    if len(parts) >= 5:
                        owner, repo = parts[3], parts[4]
                        api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/main?recursive=1"
                        async with _httpx.AsyncClient(timeout=10.0) as http:
                            tree_resp = await http.get(api_url)
                            if tree_resp.status_code == 200:
                                tree = tree_resp.json().get("tree", [])
                                auth_files = [f for f in tree if f.get("type") == "blob" and
                                    any(kw in f.get("path", "").lower() for kw in ["auth", "sign", "hmac", "credential", "signature"])]
                                # Fetch auth files (max 5)
                                for af in auth_files[:5]:
                                    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{af['path']}"
                                    async with _httpx.AsyncClient(timeout=10.0) as http2:
                                        file_resp = await http2.get(raw_url)
                                        if file_resp.status_code == 200:
                                            pages.append((f"SDK - {af['path']}", file_resp.text))
                except Exception:
                    pass

    if not pages:
        raise HTTPException(status_code=400, detail="No documentation content could be fetched.")

    # Store all pages in KB
    kb_team_id = await store_mcp_docs_kb(
        server_name=safe_name,
        api_name=request.api_name,
        pages=pages,
    )

    logger.info("mcp_fetch_docs_complete", kb_team_id=kb_team_id, pages_stored=len(pages))

    return {
        "status": "success",
        "kb_team_id": kb_team_id,
        "pages_stored": len(pages),
        "summary": f"Fetched and stored {len(pages)} pages of documentation",
    }


class MCPAnalyzeKBRequest(BaseModel):
    api_name: str
    kb_team_id: str


@router.post("/analyze-kb")
async def analyze_kb_endpoints(
    request: MCPAnalyzeKBRequest,
    session: AsyncSession = Depends(get_session)
):
    """Analyze KB contents to discover endpoints — same flow as internal teams."""
    logger.info("mcp_builder_analyze_kb_start", api_name=request.api_name, kb_team_id=request.kb_team_id)
    raise HTTPException(
        status_code=501,
        detail="MCP Builder legacy KB analysis is disabled. Use TeamKnowParse-backed training instead.",
    )

    llm_settings = await get_active_llm_settings(session)
    api_key = llm_settings.get("api_key")
    if not api_key:
        raise HTTPException(status_code=400, detail="LLM API key is required.")

    # Load KB content (clear cache first to ensure fresh data)
    from app.rag.kb_store import KnowledgeBaseStore, _kb_cache
    if request.kb_team_id in _kb_cache:
        del _kb_cache[request.kb_team_id]
    kb_store = KnowledgeBaseStore()
    kb_data = await kb_store.load(request.kb_team_id)
    if not kb_data:
        raise HTTPException(status_code=404, detail="No KB found. Fetch docs first.")

    # Extract all stored text from KB
    all_text = ""
    files = kb_data.get("files", {})
    for fname, fdata in files.items():
        if isinstance(fdata, dict):
            # KB stores content under "text" key
            all_text += fdata.get("text", fdata.get("content", "")) + "\n\n"
        elif isinstance(fdata, str):
            all_text += fdata + "\n\n"

    if not all_text.strip():
        raise HTTPException(status_code=404, detail="KB is empty. Fetch docs first.")

    # Use the SAME discovery flow as internal teams
    from app.services.mcp_builder_service import MCPBuilderService
    svc = MCPBuilderService()
    result = await svc.discover_from_text(
        api_name=request.api_name,
        api_docs_text=all_text,
        api_key=llm_settings.get("api_key", ""),
        model=llm_settings.get("model_name", "gpt-4o"),
        api_url=llm_settings.get("api_url"),
        provider=llm_settings.get("provider", "openai"),
    )

    return {
        "status": "success",
        "base_url": result.base_url,
        "auth_type": result.auth_type,
        "auth_details": getattr(result, "auth_details", None),
        "endpoints": [ep.model_dump() for ep in result.endpoints],
        "kb_team_id": request.kb_team_id,
    }


@router.post("/generate")
async def generate_mcp_server(
    request: MCPGeneratorRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    logger.info("mcp_builder_start", team_name=request.team_name, dest=request.destination_folder)

    # Compute module name early — needed for the build message and validation below
    safe_team_name = re.sub(r'[^a-zA-Z0-9]', '_', request.team_name).lower().strip('_')
    safe_team_name = re.sub(r'_+', '_', safe_team_name)
    module_name = f"{safe_team_name}_mcp"
    
    # Build destination: {user_chosen_dir}/mcp-servers/{server_name}
    # This ensures MCP files are always inside an mcp-servers subdirectory
    raw_dest = os.path.expanduser(request.destination_folder)
    raw_dest_path = Path(raw_dest)
    # If the path already ends with mcp-servers/{name}, use as-is (e.g. ~/mcp-servers/gta)
    # Otherwise, append mcp-servers/{name} to the user's chosen directory
    if raw_dest_path.parent.name == "mcp-servers":
        dest = str(raw_dest_path)
    else:
        dest = str(raw_dest_path / "mcp-servers" / safe_team_name)

    # Auto-increment folder name if it already exists (like file downloads: name, name_1, name_2)
    if os.path.exists(dest):
        base_dest = dest.rstrip('/')
        base_match = re.match(r'^(.+?)(?:_(\d+))?$', base_dest)
        base_path = base_match.group(1) if base_match else base_dest
        counter = 1
        while os.path.exists(f"{base_path}_{counter}"):
            counter += 1
        dest = f"{base_path}_{counter}"
        new_folder_name = os.path.basename(dest)
        safe_team_name = re.sub(r'[^a-zA-Z0-9]', '_', new_folder_name).lower().strip('_')
        safe_team_name = re.sub(r'_+', '_', safe_team_name)
        module_name = f"{safe_team_name}_mcp"
        logger.info("mcp_builder_auto_increment", original=request.destination_folder, new_dest=dest)

    is_external = bool(request.api_docs_url or request.api_docs_text)

    mcp_config = {
        "language": "python",
        "output_dir": request.destination_folder,
        "selected_endpoints": request.selected_endpoints
    }

    # Pass external API context to the prompt builder
    if is_external:
        mcp_config["api_docs_url"] = request.api_docs_url
        mcp_config["api_base_url"] = request.api_base_url
        mcp_config["auth_type"] = request.auth_type
        mcp_config["auth_details"] = request.auth_details
        
        # Always embed the API docs in the build message so the agent reads
        # them straight from the task. External builds never route through a
        # knowledge base — a KB lookup depends on the OpenCode sidecar reloading
        # its team scope, which does not happen reliably.
        docs_text = request.api_docs_text
        if docs_text:
            sliced_spec = mcp_builder_service.slice_openapi_for_endpoints(docs_text, request.selected_endpoints)
            if sliced_spec:
                mcp_config["api_docs_text"] = sliced_spec
            else:
                mcp_config["api_docs_text"] = docs_text[:50000] + ("..." if len(docs_text) > 50000 else "")
        elif request.api_docs_url:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10.0) as http:
                    resp = await http.get(request.api_docs_url)
                    sliced_spec = mcp_builder_service.slice_openapi_for_endpoints(resp.text, request.selected_endpoints)
                    if sliced_spec:
                        mcp_config["api_docs_text"] = sliced_spec
                        mcp_config["api_docs_url"] = None
            except Exception:
                pass

        # Fallback: build structured summary from selected endpoints
        if not mcp_config.get("api_docs_text") and request.selected_endpoints:
            ep_lines = []
            for ep in request.selected_endpoints:
                line = f"{ep.get('method', 'GET')} {ep.get('path', '/')} — {ep.get('description', '')}"
                params = ep.get("parameters", [])
                if params:
                    param_strs = []
                    for p in params:
                        req = " (required)" if p.get("required") else ""
                        param_strs.append(f"  - {p.get('name')}: {p.get('type', 'string')}{req} [{p.get('location', 'body')}]")
                    line += "\n" + "\n".join(param_strs)
                ep_lines.append(line)
            auth_section = f"Auth Type: {request.auth_type or 'bearer'}"
            if request.auth_details:
                auth_section += f"\nAuth Details: {request.auth_details}"
            endpoint_summary = (
                f"API Base URL: {request.api_base_url or 'unknown'}\n"
                f"{auth_section}\n\n"
                f"Endpoints:\n\n" + "\n\n".join(ep_lines)
            )
            mcp_config["api_docs_text"] = endpoint_summary
            mcp_config["api_docs_url"] = None

    def _sse(event: str, data: Any) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    def _extract_mcp_config(final_answer_text: str) -> dict:
        """Build MCP config by inspecting what the agent actually generated on disk.

        1. Read pyproject.toml to find the real module name
        2. Scan server.py for os.environ/os.getenv calls to discover real env var names
        3. Fall back to conventions only if files are missing
        """
        expanded_dest = os.path.expanduser(dest)

        # ── 1. Discover real module name from pyproject.toml ──
        actual_module = module_name  # fallback
        pyproject_path = os.path.join(expanded_dest, "pyproject.toml")
        if os.path.isfile(pyproject_path):
            try:
                with open(pyproject_path) as f:
                    content = f.read()
                # Look for: module_name.server:main  in [project.scripts]
                m = re.search(r'=\s*"([a-zA-Z0-9_]+)\.server:main"', content)
                if m:
                    actual_module = m.group(1)
            except Exception:
                pass

        # ── 2. Find server.py — check both src/ layout and flat layout ──
        server_path = None
        for candidate in [
            os.path.join(expanded_dest, "src", actual_module, "server.py"),
            os.path.join(expanded_dest, actual_module, "server.py"),
            os.path.join(expanded_dest, "server.py"),
        ]:
            if os.path.isfile(candidate):
                server_path = candidate
                break

        # ── 3. Extract env var names from actual server.py ──
        env_vars: dict = {}
        if server_path:
            try:
                with open(server_path) as f:
                    server_src = f.read()
                # Match os.environ["KEY"], os.environ.get("KEY"), os.getenv("KEY")
                env_matches = re.findall(
                    r'os\.(?:environ(?:\.get)?\s*\[\s*["\']|environ\.get\s*\(\s*["\']|getenv\s*\(\s*["\'])([A-Z][A-Z0-9_]*)',
                    server_src,
                )
                for var_name in dict.fromkeys(env_matches):  # dedupe, preserve order
                    env_vars[var_name] = f"<your-{var_name.lower()}>"
            except Exception:
                pass

        # Fallback: if no env vars found but auth is needed, use convention
        if not env_vars:
            auth = getattr(request, 'auth_type', None) or ''
            if auth and auth.lower() not in ('none', '', 'no auth', 'no_auth'):
                env_vars = {f"{safe_team_name.upper()}_API_TOKEN": "<your-token>"}

        # Also check agent's JSON output for env vars we might have missed
        match = re.search(r'```json\n(.*?)\n```', final_answer_text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                servers = parsed.get("mcpServers", parsed)
                for srv_config in servers.values():
                    if isinstance(srv_config, dict) and "env" in srv_config:
                        # Merge — agent env vars fill in any we missed
                        for k, v in srv_config["env"].items():
                            if k not in env_vars:
                                env_vars[k] = v
                        break
            except Exception:
                pass

        # ── 4. Determine command — prefer .venv if it exists, else uv run ──
        venv_python = os.path.join(expanded_dest, ".venv", "bin", "python")
        if os.path.isfile(venv_python):
            command = venv_python
            args = ["-m", f"{actual_module}.server"]
        else:
            command = "uv"
            args = ["run", "--directory", expanded_dest, "python", "-m", f"{actual_module}.server"]

        config = {
            safe_team_name: {
                "command": command,
                "args": args,
            }
        }
        if env_vars:
            config[safe_team_name]["env"] = env_vars
        return config

    def _build_quick_start() -> str:
        expanded_dest = os.path.expanduser(dest)

        # Discover actual module name (same logic as config)
        actual_module = module_name
        pyproject_path = os.path.join(expanded_dest, "pyproject.toml")
        if os.path.isfile(pyproject_path):
            try:
                with open(pyproject_path) as f:
                    content = f.read()
                m = re.search(r'=\s*"([a-zA-Z0-9_]+)\.server:main"', content)
                if m:
                    actual_module = m.group(1)
            except Exception:
                pass

        venv_python = os.path.join(expanded_dest, ".venv", "bin", "python")
        if os.path.isfile(venv_python):
            run_cmd = f"{venv_python} -m {actual_module}.server"
        else:
            run_cmd = f"uv run --directory {dest} python -m {actual_module}.server"

        return (
            f"# Step 1: Install dependencies (only needed once)\n"
            f"cd {dest}\n"
            f"rm -f uv.lock\n"
            f"uv sync\n"
            f"\n"
            f"# Step 2: Start the MCP server\n"
            f"{run_cmd}\n"
            f"\n"
            f"# Step 3 (optional): Test with MCP Inspector\n"
            f"npx @modelcontextprotocol/inspector {run_cmd}"
        )

    # For external APIs with a KB, use the KB team_id so the agent gets
    # knowledge_base tool access to search the crawled/uploaded docs.
    agent_team_id = request.kb_team_id if request.kb_team_id else request.team_id

    state = {"final_answer": ""}

    async def _run_via_opencode(message: str, max_steps: int):
        """Run the build through the OpenCode `mcp-builder` agent.

        Creates an OC session (or reuses `state['oc_session_id']` so the retry
        sees its prior file writes), sends the build message, and relays OC's
        SSE event stream as MCP Builder SSE events. Sets `state['final_answer']`
        to the assistant's full text when the turn finishes.

        OC event → MCP Builder event mapping:
          message.part.updated text → agent_answer_chunk {done:false}
          message.part.updated tool → agent_status + tool_output
          message.updated finish=stop → agent_answer_chunk {done:true}
          session.error → agent_status {step:error}
        """
        import httpx as _httpx
        from app.services.opencode_manager import opencode_manager

        try:
            port = await opencode_manager.start_for_user(
                current_user.user_id, team_id=agent_team_id
            )
        except Exception as e:
            logger.error("mcp_builder_oc_start_failed", error=str(e))
            yield _sse("agent_status", {"step": "error", "message": f"OpenCode start failed: {e}"})
            return

        async with _httpx.AsyncClient(timeout=_httpx.Timeout(600.0, connect=10.0)) as client:
            # Create the build session once; the retry reuses it so the agent
            # sees the files it already wrote.
            oc_session_id = state.get("oc_session_id")
            if not oc_session_id:
                try:
                    resp = await client.post(
                        f"http://127.0.0.1:{port}/session",
                        json={"agent": "mcp-builder"},
                    )
                    if resp.status_code != 200:
                        yield _sse("agent_status", {"step": "error", "message": f"OC session create failed: {resp.status_code}"})
                        return
                    oc_session_id = resp.json().get("id")
                    state["oc_session_id"] = oc_session_id
                    logger.info("mcp_builder_oc_session", session_id=oc_session_id, port=port)
                except Exception as e:
                    yield _sse("agent_status", {"step": "error", "message": f"OC session create error: {e}"})
                    return

            # Subscribe to the event stream BEFORE firing the prompt so no
            # events are missed.
            try:
                event_stream = client.stream(
                    "GET", f"http://127.0.0.1:{port}/global/event",
                    headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
                )
                event_resp = await event_stream.__aenter__()
            except Exception as e:
                yield _sse("agent_status", {"step": "error", "message": f"OC event subscribe failed: {e}"})
                return

            text_parts: dict[str, str] = {}
            message_roles: dict[str, str] = {}
            try:
                async_resp = await client.post(
                    f"http://127.0.0.1:{port}/session/{oc_session_id}/prompt_async",
                    json={"parts": [{"type": "text", "text": message}]},
                )
                if async_resp.status_code not in (200, 204):
                    yield _sse("agent_status", {"step": "error", "message": f"OC prompt_async failed: {async_resp.status_code}"})
                    return

                async for line in event_resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        wrapper = json.loads(data_str)
                    except Exception:
                        continue
                    payload = wrapper.get("payload", wrapper)
                    etype = payload.get("type", "")
                    props = payload.get("properties", {})

                    evt_sid = props.get("sessionID", "")
                    if evt_sid and evt_sid != oc_session_id:
                        continue

                    if etype == "message.updated":
                        info = props.get("info", {})
                        iid = info.get("id", "")
                        irole = info.get("role", "")
                        if iid and irole:
                            message_roles[iid] = irole
                        # `finish == "stop"` marks the end of the whole turn
                        # (intermediate tool-use steps have other finish reasons).
                        if irole == "assistant" and info.get("finish") == "stop":
                            final_text = "".join(text_parts.values())
                            state["final_answer"] = final_text
                            yield _sse("agent_answer_chunk", {"chunk": final_text, "done": True})
                            return

                    elif etype == "message.part.updated":
                        part = props.get("part", {})
                        msg_id = props.get("messageID", "") or part.get("messageID", "")
                        if message_roles.get(msg_id) != "assistant":
                            continue
                        ptype = part.get("type")
                        if ptype == "text":
                            text_parts[part.get("id", "")] = part.get("text", "")
                            yield _sse("agent_answer_chunk", {"chunk": part.get("text", ""), "done": False})
                        elif ptype == "tool":
                            tstate = part.get("state", {})
                            tool_name = part.get("tool", "")
                            yield _sse("agent_status", {"step": "tool", "message": f"Running {tool_name}"})
                            out = tstate.get("output", "")
                            if out:
                                yield _sse("tool_output", {"output": str(out)[:2000], "tool": tool_name})

                    elif etype == "session.error":
                        err = props.get("error", {}) or {}
                        emsg = err.get("message") or (err.get("data") or {}).get("message") or "Session error"
                        logger.warning("mcp_builder_oc_session_error", message=str(emsg)[:200])
                        yield _sse("agent_status", {"step": "error", "message": str(emsg)})
                        return
            except Exception as e:
                logger.error("mcp_builder_oc_relay_error", error=str(e))
                yield _sse("agent_status", {"step": "error", "message": f"OpenCode relay error: {e}"})
            finally:
                try:
                    await event_stream.__aexit__(None, None, None)
                except Exception:
                    pass

    def _rescue_extract_server_code(answer: str) -> bool:
        """Last resort: if the agent dumped server code into its response text,
        extract it and write it to disk ourselves. Returns True if rescued."""
        if not answer:
            return False

        server_path = os.path.expanduser(f"{dest}/src/{module_name}/server.py")
        init_path = os.path.expanduser(f"{dest}/src/{module_name}/__init__.py")
        pyproject_path = os.path.expanduser(f"{dest}/pyproject.toml")

        # Look for Python code blocks that look like server code
        # Handle both closed (```python...```) and unclosed (```python... EOF) blocks
        code_blocks = re.findall(r'```python\s*\n(.*?)```', answer, re.DOTALL)

        # Also try unclosed blocks — agent often dumps code that gets truncated
        unclosed = re.findall(r'```python\s*\n(.+)', answer, re.DOTALL)
        for block in unclosed:
            # Remove any trailing ``` if partially present
            block = block.rstrip('`').strip()
            if block and block not in code_blocks:
                code_blocks.append(block)

        # Find the largest code block that looks like server code
        # (contains imports + function definitions — doesn't have to use FastMCP)
        best_block = None
        best_score = 0
        for block in code_blocks:
            score = 0
            if "import " in block:
                score += 1
            if "def " in block:
                score += 1
            if "@mcp.tool" in block:
                score += 10
            if "FastMCP" in block or "fastmcp" in block:
                score += 10
            if "httpx" in block or "AsyncClient" in block:
                score += 2
            if "async def" in block:
                score += 2
            if len(block) > 500:
                score += 3
            if score > best_score:
                best_score = score
                best_block = block

        if best_block and best_score >= 3:
            os.makedirs(os.path.dirname(server_path), exist_ok=True)
            with open(server_path, "w") as f:
                f.write(best_block)
            if not os.path.exists(init_path):
                with open(init_path, "w") as f:
                    f.write("")
            logger.info("mcp_builder_rescue_extracted", path=server_path, code_len=len(best_block), score=best_score)

            # Also look for pyproject.toml in the code blocks
            for block in code_blocks:
                if "[build-system]" in block or "[project]" in block:
                    if not os.path.exists(pyproject_path):
                        with open(pyproject_path, "w") as f:
                            f.write(block)
                        logger.info("mcp_builder_rescue_pyproject", path=pyproject_path)
                    break

            return True

        # Also check if the entire answer looks like Python code (no markdown)
        stripped = answer.strip()
        if stripped.startswith(("import ", "from ")) and ("def " in stripped) and len(stripped) > 500:
            os.makedirs(os.path.dirname(server_path), exist_ok=True)
            with open(server_path, "w") as f:
                f.write(stripped)
            if not os.path.exists(init_path):
                with open(init_path, "w") as f:
                    f.write("")
            logger.info("mcp_builder_rescue_raw_code", path=server_path, code_len=len(stripped))
            return True

        return False

    async def _validate_server() -> tuple[str | None, str]:
        """Validate the generated server. Returns (error_or_None, severity).

        severity: "missing" = server.py doesn't exist at all (needs full rewrite)
                  "syntax"  = server.py exists but has syntax/import errors
                  "incomplete" = server.py exists but has no tool functions
                  "ok"      = all checks passed
        """
        import subprocess
        import sys
        server_path = os.path.expanduser(f"{dest}/src/{module_name}/server.py")

        if not os.path.exists(server_path):
            return f"Server file not found at {server_path}", "missing"

        # Check file is not empty/stub
        try:
            content = open(server_path).read()
            if len(content.strip()) < 100:
                return f"Server file at {server_path} is essentially empty ({len(content)} chars)", "missing"
            # Count @mcp.tool() decorators as a proxy for implemented tools
            tool_count = content.count("@mcp.tool")
            if tool_count == 0:
                return f"Server file exists but contains 0 @mcp.tool() functions. Expected {len(request.selected_endpoints)} tools.", "incomplete"
        except Exception as e:
            return str(e), "syntax"

        # Syntax check
        try:
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", server_path],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return result.stderr.strip(), "syntax"
        except Exception as e:
            return str(e), "syntax"

        return None, "ok"

    def _mirror_to_progress(event: str):
        """Parse an SSE event string and write relevant fields to the progress store."""
        from app.services.mcp_builder_progress import update_progress
        try:
            if not event or "\ndata: " not in event:
                return
            data_line = event.split("\ndata: ", 1)[1].split("\n", 1)[0]
            data = json.loads(data_line)

            if event.startswith("event: agent_status"):
                update_progress(safe_team_name, phase=data.get("message") or data.get("step", ""))
            elif event.startswith("event: agent_tools"):
                update_progress(safe_team_name, phase=f"{data.get('count', 0)} tools loaded")
            elif event.startswith("event: agent_code"):
                if "iteration" in data:
                    update_progress(safe_team_name, iteration=data["iteration"])
            elif event.startswith("event: tool_output"):
                if data.get("output"):
                    short = data["output"][:120].replace("\n", " ")
                    update_progress(safe_team_name, log=short)
        except Exception:
            pass  # Never let mirroring break the SSE stream

    async def _stream_generation():
        from app.services.mcp_builder_progress import update_progress
        state["final_answer"] = ""
        update_progress(safe_team_name, status="in_progress", phase="Starting generation...")

        # ── Attempt 1: Full generation ────────────────────────────
        from app.skills.mcp_builder_oc_prompt import build_mcp_builder_oc_message
        oc_message = build_mcp_builder_oc_message(
            output_dir=dest,
            service_name=safe_team_name,
            is_external=is_external,
            has_kb=False,  # external builds embed docs in the message — never KB research
            api_docs_text=mcp_config.get("api_docs_text"),
            api_base_url=request.api_base_url,
            auth_type=request.auth_type,
            auth_details=request.auth_details,
            selected_endpoints=request.selected_endpoints,
        )
        async for event in _run_via_opencode(oc_message, max_steps=80):
            _mirror_to_progress(event)
            yield event

        final_answer = state["final_answer"]

        # ── Try rescue extraction before validation ─────────────
        # If agent dumped code into its response, save it to disk
        _rescue_extract_server_code(final_answer)

        # ── Server-side validation ────────────────────────────────
        validation_error, severity = await _validate_server()

        if validation_error:
            logger.warning("mcp_builder_validation_failed", error=validation_error, severity=severity)
            yield _sse("agent_status", {
                "step": "retry",
                "message": f"Validation failed ({severity}), auto-retrying: {validation_error[:150]}"
            })

            # Build retry prompt based on severity
            if severity == "missing":
                # Server.py doesn't exist — agent needs to write everything
                endpoint_names = ", ".join(
                    ep.get("suggested_tool_name", ep.get("path", "?"))
                    for ep in request.selected_endpoints[:20]
                )
                retry_prompt = (
                    f"CRITICAL: The MCP server generation FAILED. The file {dest}/src/{module_name}/server.py "
                    f"does NOT exist. You MUST write it now.\n\n"
                    f"Create the COMPLETE server at {dest}/src/{module_name}/server.py with:\n"
                    f"1. `from fastmcp import FastMCP` and `mcp = FastMCP(\"{safe_team_name}_mcp\")`\n"
                    f"2. One @mcp.tool() function for EACH of these tools: {endpoint_names}\n"
                    f"3. Each tool must make a real HTTP request using httpx.AsyncClient\n"
                    f"4. Also ensure {dest}/src/{module_name}/__init__.py exists\n"
                    f"5. Run `cd {dest} && uv sync` after writing the files\n\n"
                    f"Use the `write` tool to create each file on disk. "
                    f"DO NOT output code into your response text — all code must be saved to files."
                )
            elif severity == "incomplete":
                retry_prompt = (
                    f"The server file at {dest}/src/{module_name}/server.py exists but contains "
                    f"0 @mcp.tool() functions. It needs {len(request.selected_endpoints)} tools.\n\n"
                    f"Error: {validation_error}\n\n"
                    f"Read the current file, then add ALL missing @mcp.tool() functions. "
                    f"Each tool must make a real HTTP request. Do NOT write empty stubs."
                )
            else:
                # syntax error
                retry_prompt = (
                    f"The MCP server at {dest} has a syntax/import error:\n\n"
                    f"```\n{validation_error}\n```\n\n"
                    f"Read the file, fix the error with the `edit` tool, then verify with "
                    f"`py_compile` and an import check."
                )

            update_progress(safe_team_name, phase=f"Retrying ({severity})...")
            # Reuses the same OC session (state['oc_session_id']) so the retry
            # agent sees the files it already wrote.
            async for event in _run_via_opencode(retry_prompt, max_steps=40):
                _mirror_to_progress(event)
                yield event
            final_answer = state["final_answer"] or final_answer

            # Re-validate after retry
            validation_error_2, severity_2 = await _validate_server()
            if validation_error_2:
                logger.error("mcp_builder_retry_also_failed", error=validation_error_2, severity=severity_2)

                # Last resort: try to extract server code from the agent's response
                if severity_2 in ("missing", "incomplete"):
                    rescued = _rescue_extract_server_code(final_answer)
                    if rescued:
                        yield _sse("agent_status", {
                            "step": "rescue",
                            "message": "Extracted server code from agent response and saved to disk."
                        })

        # ── Final validation status ───────────────────────────────
        final_error, final_severity = await _validate_server()

        if final_error and final_severity == "missing":
            # Total failure — server.py never got created
            logger.error("mcp_builder_total_failure", error=final_error)
            fail_msg = (f"Generation failed: {final_error}. The LLM agent did not produce the server code. "
                        f"Please check your LLM API configuration and try again.")
            update_progress(safe_team_name, status="failed", phase="Generation failed",
                            result={"status": "error", "message": fail_msg})
            yield _sse("mcp_result", {
                "status": "error",
                "message": fail_msg,
                "mcpServers": None,
                "quickStartCommands": None,
            })
            return

        # ── Emit final result ─────────────────────────────────────
        mcp_servers_config = _extract_mcp_config(final_answer)
        status = "success" if not final_error else "warning"
        message = "Generation complete." if not final_error else f"Generation complete with warnings: {final_error}"

        logger.info("mcp_builder_complete", config=mcp_servers_config, status=status)

        # ── Install dependencies with uv sync ─────────────────────
        try:
            import subprocess as _sp
            uv_bin = os.path.expanduser("~/.local/bin/uv")
            if not os.path.exists(uv_bin):
                uv_bin = "uv"  # fallback to PATH
            uv_result = _sp.run(
                [uv_bin, "sync"],
                cwd=dest,
                capture_output=True, text=True, timeout=120,
            )
            if uv_result.returncode == 0:
                logger.info("mcp_builder_uv_sync_ok", dest=dest)
            else:
                logger.warning("mcp_builder_uv_sync_failed", stderr=uv_result.stderr[:500])
        except Exception as uv_err:
            logger.warning("mcp_builder_uv_sync_error", error=str(uv_err))

        # ── Save to registry ─────────────────────────────────────
        oc_reload_needed = False
        try:
            from app.models.mcp_server import McpServer
            source_type = "internal"
            if is_external:
                if request.api_docs_url:
                    source_type = "external_url"
                elif request.api_docs_text:
                    source_type = "external_text"
                else:
                    source_type = "external_upload"

            new_server = McpServer(
                name=safe_team_name,
                team_name=request.team_name,
                destination_folder=dest,
                module_name=module_name,
                mcp_config_json=mcp_servers_config,
                quick_start_commands=_build_quick_start(),
                selected_endpoints_json=request.selected_endpoints,
                source_type=source_type,
                api_docs_url=request.api_docs_url,
                api_base_url=request.api_base_url,
                auth_type=request.auth_type,
                kb_team_id=request.kb_team_id,
                created_by=current_user.user_id,
            )
            session.add(new_server)
            await session.commit()
            logger.info("mcp_server_saved", server_id=new_server.server_id, name=safe_team_name)

            # Bridge: auto-create extension + user install so it appears in My Extensions
            try:
                from app.models.mcp_extension import McpExtension
                from app.models.user_extension import UserExtension

                # Use the venv python if uv sync created it, otherwise use uv run
                venv_python = os.path.join(dest, ".venv", "bin", "python")
                use_venv = os.path.exists(venv_python)

                # Extract expected env var names from generated server.py
                # so the Edit dialog shows pre-filled field names for the user
                expected_env_vars = {}
                try:
                    import re as _re
                    server_py_path = os.path.join(dest, "src", module_name, "server.py")
                    if os.path.isfile(server_py_path):
                        with open(server_py_path, "r") as f:
                            server_code = f.read()
                        # Match os.getenv("KEY"), os.getenv("KEY", default), os.environ["KEY"], os.environ.get("KEY")
                        env_patterns = [
                            r'os\.getenv\(\s*["\']([A-Z_][A-Z0-9_]*)["\']',
                            r'os\.environ\[\s*["\']([A-Z_][A-Z0-9_]*)["\']',
                            r'os\.environ\.get\(\s*["\']([A-Z_][A-Z0-9_]*)["\']',
                        ]
                        for pattern in env_patterns:
                            for match in _re.finditer(pattern, server_code):
                                var_name = match.group(1)
                                expected_env_vars[var_name] = ""
                        logger.info("mcp_bridge_env_vars_extracted", count=len(expected_env_vars), vars=list(expected_env_vars.keys()))
                except Exception as env_err:
                    logger.warning("mcp_bridge_env_vars_extract_failed", error=str(env_err))

                async with async_session_maker() as bridge_session:
                    if use_venv:
                        mcp_command = venv_python
                        mcp_args = ["-m", f"{module_name}.server"]
                    else:
                        mcp_command = "uv"
                        mcp_args = ["run", "--directory", dest, "python", "-m", f"{module_name}.server"]

                    ext = McpExtension(
                        name=request.team_name,
                        description=f"Auto-generated MCP server for {request.team_name}",
                        command=mcp_command,
                        args_json=mcp_args,
                        env_vars_json=expected_env_vars if expected_env_vars else {},
                        auth_type=request.auth_type or "none",
                        is_builtin=False,
                        mcp_server_id=new_server.server_id,
                        created_by=current_user.user_id,
                        tenant_id=current_user.tenant_id,
                        is_enabled=True,
                    )
                    bridge_session.add(ext)
                    await bridge_session.flush()

                    # Auto-install for the creator
                    user_ext = UserExtension(
                        extension_id=ext.extension_id,
                        user_id=current_user.user_id,
                        tenant_id=current_user.tenant_id,
                        is_enabled=True,
                    )
                    bridge_session.add(user_ext)

                    # Auto-install for users managed by this user_admin
                    if current_user.role == "user_admin":
                        from app.models.user import User
                        managed_users_result = await bridge_session.execute(
                            select(User.user_id).where(
                                User.manager_id == current_user.user_id,
                            )
                        )
                        for (uid,) in managed_users_result.all():
                            bridge_session.add(UserExtension(
                                extension_id=ext.extension_id,
                                user_id=uid,
                                tenant_id=current_user.tenant_id,
                                is_enabled=True,
                            ))

                    await bridge_session.commit()
                    logger.info("mcp_builder_bridge_created", extension_id=ext.extension_id, user_id=current_user.user_id)
                    oc_reload_needed = True
            except Exception as bridge_exc:
                logger.warning("mcp_builder_bridge_failed", error=str(bridge_exc))

        except Exception as exc:
            logger.warning("mcp_server_save_failed", error=str(exc))

        # ── Hand the new server to the agent ─────────────────────
        # The McpExtension/UserExtension rows exist now; restart the user's
        # OpenCode so its config regenerates and the /agent chat picks up the
        # new MCP server. Best-effort — a reload failure must not fail a build
        # that otherwise succeeded.
        if oc_reload_needed:
            try:
                yield _sse("agent_status", {
                    "step": "activate",
                    "message": "Activating server for the agent...",
                })
                from app.services.opencode_manager import opencode_manager
                await opencode_manager.reload_for_user(current_user.user_id)
            except Exception as reload_exc:
                logger.warning("mcp_builder_oc_reload_failed", error=str(reload_exc))

        update_progress(safe_team_name, status="completed", phase="Complete",
                        result={"status": status, "mcpServers": mcp_servers_config,
                                "quickStartCommands": _build_quick_start(), "message": message})
        yield _sse("mcp_result", {
            "status": status,
            "mcpServers": mcp_servers_config,
            "quickStartCommands": _build_quick_start(),
            "message": message,
        })

    return StreamingResponse(
        _stream_generation(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
