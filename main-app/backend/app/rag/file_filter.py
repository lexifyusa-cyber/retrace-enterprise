"""
FileFilterService — Phase 2: LLM project analysis + keep/exclude filtering.

Optional on_debug_prompt(step_name, system_content, user_content, batch_idx=None, total_batches=None)
is called before each LLM request when provided (e.g. when debug logging is enabled).

Two-step LLM evaluation:
  Step 1 — FOLDER evaluation: LLM sees the complete directory tree and
           decides which folders to EXCLUDE.  Also produces a project-level
           analysis (summary, technologies, architecture, components).
  Step 2 — FILE evaluation: for files in surviving folders, the LLM
           decides keep/exclude and assigns content_type + rich metadata
           (description, component, role, relationships, importance).

The LLM never sees file contents — only names, paths, extensions, sizes,
dates, and Phase 1 tags plus the product description.

ALL analysis data is stored permanently in the knowledge base.
"""

import json
from typing import Callable, Optional

import structlog
from openai import AsyncOpenAI

from app.rag.models import TreeNode, ProjectAnalysis, ComponentInfo

logger = structlog.get_logger()

FILTER_BATCH_SIZE = 800

# ---------------------------------------------------------------------------
# Step 1 system prompt — folder-level analysis + exclusion
# ---------------------------------------------------------------------------

_FOLDER_SYSTEM_PROMPT = """\
You are a senior software architect analysing a project for a knowledge base.

You will receive the COMPLETE list of every FOLDER in the project — one line
per folder with path, file count, and total size.  No individual filenames are
listed; this compact list ensures you see every folder (e.g. venv, node_modules,
logs) even in large codebases.  Use path, file count, and size to decide.

Your TWO jobs:

═══════════════════════════════════════════════════════════════
JOB 1 — PROJECT ANALYSIS
═══════════════════════════════════════════════════════════════
Analyse the project and produce:
  • summary: 2-3 sentence description of what this project is
  • technologies: list of languages, frameworks, tools detected
  • architecture_style: e.g. "Monolithic", "Microservices", "SPA + API", etc.
  • key_components: list of logical components with name, description, primary_folders

═══════════════════════════════════════════════════════════════
JOB 2 — FOLDER DECISIONS
═══════════════════════════════════════════════════════════════
For EVERY folder in the tree, decide:
  • decision: "keep" or "excluded"
  • purpose: what this folder contains (1 sentence)
  • technologies: technologies used in this folder
  • importance: "critical" | "high" | "medium" | "low"
  • reason: why excluded (only if excluded)

IMPORTANT — CASCADE RULE:
When you EXCLUDE a folder, ALL files and subfolders inside it are
excluded automatically.  You do NOT need to list any sub-item of an
excluded folder — just exclude the top-level folder and everything
inside is removed.  Only provide decisions for the folder itself.

EXCLUDE these folder types:
  Third-party dependencies:
    node_modules, .npm, .yarn, bower_components, venv, .venv, env,
    site-packages, lib/python*, __pycache__, .eggs, *.egg-info,
    .gradle, .m2, target (Maven), packages, .nuget, vendor (Go/PHP/Ruby),
    Pods, Carthage, DerivedData, deps, _build, .stack-work,
    .dart_tool, .pub-cache, renv/library, packrat/lib

  Build output:
    dist, build, out, output, bin (compiled), obj, Debug, Release,
    .next, .nuxt, .angular, .svelte-kit, cmake-build-*,
    __generated__, generated, _site, site (static output)

  Caches & temp:
    .cache, .tmp, tmp, temp, .pytest_cache, .mypy_cache, .ruff_cache,
    .tox, .turbo, .parcel-cache, .webpack, .eslintcache, logs, log

  IDE state:
    .idea, .vscode, .vs, .eclipse, .settings, xcuserdata

  Version control internals:
    .git, .svn, .hg, .bzr

  Test output:
    coverage, htmlcov, .nyc_output, test-results, TestResults

  Infrastructure state (not config):
    .terraform, terraform.tfstate.d

KEEP folders that contain actual source code, documentation, configuration,
scripts, architecture docs, API specs, database schemas, deployment configs,
CI/CD pipelines, or any content that helps understand the product.

This applies to ANY kind of project: Java, .NET, C#, COBOL, Python, React,
Angular, SAP, FICO, financial products, installed desktop apps, documentation
repositories, data pipelines, mobile apps, embedded systems, or anything else.

All paths in the tree are RELATIVE (the selected root folder is "/").
Use the SAME relative paths in your response.

Respond with ONLY valid JSON (no markdown fences):
{
  "project_analysis": {
    "summary": "...",
    "technologies": ["..."],
    "architecture_style": "...",
    "key_components": [
      {"name": "...", "description": "...", "primary_folders": ["..."]}
    ]
  },
  "folders": {
    "/": {
      "decision": "keep",
      "purpose": "Project root",
      "technologies": ["..."],
      "importance": "high"
    },
    "/src/": {
      "decision": "keep",
      "purpose": "...",
      "technologies": ["..."],
      "importance": "high"
    },
    "/node_modules/": {
      "decision": "excluded",
      "reason": "Third-party npm dependencies"
    }
  }
}
"""

# ---------------------------------------------------------------------------
# Step 2 system prompt — file-level analysis + content_type assignment
# ---------------------------------------------------------------------------

_FILE_SYSTEM_PROMPT = """\
You are a senior software architect analysing individual files for a
knowledge base.  You already know the project context (provided below).

Files from folders that were already excluded in the folder-evaluation
step are NOT in the list — they have already been removed.  You only
see files from KEPT folders.  An "EXCLUDED FOLDERS" section is provided
for context so you understand the project structure.

For EVERY file in the list, decide:
  • decision: "keep" or "excluded"
  • content_type: "text" | "image" | "text_or_image"
      - "text": source code, scripts, config, markdown, CSV, JSON, YAML, XML, etc.
      - "image": PNG, JPG, SVG, GIF, BMP, TIFF, etc.
      - "text_or_image": PDF, DOCX, PPTX, XLSX, Visio, EPUB, RTF, etc.
        (files that could contain text, images, or both)
  • description: 1-sentence description of what this file likely does/contains
  • component: which component or module this belongs to (lowercase)
  • role: the role this file plays (e.g. "entry point", "config", "test", "utility")
  • relationships: list of other filenames this file likely relates to
  • importance: "critical" | "high" | "medium" | "low"

EXCLUDE files that are:
  • Empty or near-empty (<50 bytes)
  • __init__.py with no content
  • Lock files (package-lock.json, yarn.lock, Gemfile.lock, etc.)
  • Auto-generated files (*.min.js, *.min.css, sourcemaps)
  • Compiled output accidentally present
  • Files unlikely to answer any question about how the product works

Files already tagged [PHASE1_EXCLUDED] → return as decision "excluded"
with content_type "excluded".

All paths are RELATIVE (the selected root folder is "/").
Use the SAME relative paths in your response keys.

For kept files, be thorough with descriptions — these descriptions become
searchable in the knowledge base and directly improve retrieval quality.

Respond with ONLY valid JSON (no markdown fences):
{
  "files": {
    "/src/app.py": {
      "decision": "keep",
      "content_type": "text",
      "description": "Main application entry point with FastAPI routes and middleware",
      "component": "api",
      "role": "entry point",
      "relationships": ["config.py", "database.py"],
      "importance": "critical"
    },
    "/styles.css": {
      "decision": "excluded",
      "content_type": "excluded",
      "reason": "Stylesheet with no business logic"
    }
  }
}
"""


class FileFilterService:
    """Phase 2: LLM-driven project analysis, folder/file filtering, and metadata."""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        kw: dict = {"api_key": api_key}
        if base_url:
            kw["base_url"] = base_url
        self._client = AsyncOpenAI(**kw)
        self._model = model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyse_and_filter(
        self,
        trees: list[TreeNode],
        product_name: str,
        product_description: str,
        on_progress=None,
        on_debug_prompt: Optional[Callable[[str, str, str, Optional[int], Optional[int]], None]] = None,
    ) -> ProjectAnalysis:
        """Run two-step LLM evaluation on the trees.

        Mutates tree nodes in-place with decisions, content_type, descriptions.
        Returns the ProjectAnalysis object.
        When on_debug_prompt is set, it is called before each LLM request with
        (step_name, system_content, user_content, batch_idx=None, total_batches=None).
        """
        # Step 1 — Folder evaluation (folder-only summary so LLM sees every folder, no truncation)
        if on_progress:
            await on_progress("Building folder list for LLM…")

        self._build_path_mappings(trees)
        folder_summary_text = self._serialize_folder_summary(trees)
        project_analysis = await self._step1_folders(
            folder_summary_text, product_name, product_description, on_progress,
            on_debug_prompt=on_debug_prompt,
        )

        # Apply folder decisions to tree nodes so _collect_surviving_files
        # only returns files from kept folders (not the entire tree).
        self.apply_folder_decisions_to_trees(trees)

        # Step 2 — File evaluation (only files in kept folders)
        surviving_files = []
        for tree in trees:
            surviving_files.extend(self._collect_surviving_files(tree))

        if on_progress:
            await on_progress(
                f"Phase 2b: Evaluating {len(surviving_files)} files with LLM…"
            )

        if surviving_files:
            await self._step2_files(
                surviving_files, project_analysis, product_name,
                product_description, on_progress,
                on_debug_prompt=on_debug_prompt,
            )

        return project_analysis

    # ------------------------------------------------------------------
    # Step 1: Folder evaluation
    # ------------------------------------------------------------------

    async def _step1_folders(
        self,
        tree_text: str,
        product_name: str,
        product_description: str,
        on_progress=None,
        on_debug_prompt: Optional[Callable[[str, str, str, Optional[int], Optional[int]], None]] = None,
    ) -> ProjectAnalysis:
        """Send tree to LLM for folder decisions + project analysis."""
        user_msg = (
            f"=== PRODUCT ===\n"
            f"Name: {product_name}\n"
            f"Description: {product_description or '(none provided)'}\n\n"
            f"=== COMPLETE DIRECTORY TREE ===\n"
            f"{tree_text}\n"
        )

        if on_progress:
            folder_count = tree_text.count("\n") + (1 if tree_text.strip() else 0)
            await on_progress(
                f"Phase 2a: Sending folder list ({folder_count} folders) to LLM for analysis…"
            )

        if on_debug_prompt:
            on_debug_prompt("folder", _FOLDER_SYSTEM_PROMPT, user_msg, None, None)

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _FOLDER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)
        except Exception as exc:
            logger.error("phase2_folder_llm_failed", error=str(exc))
            if on_progress:
                await on_progress(f"⚠️ LLM folder analysis failed: {str(exc)[:80]}")
            return ProjectAnalysis()

        # Parse project analysis
        pa_raw = data.get("project_analysis", {})
        project_analysis = ProjectAnalysis(
            summary=pa_raw.get("summary", ""),
            technologies=pa_raw.get("technologies", []),
            architecture_style=pa_raw.get("architecture_style", ""),
            key_components=[
                ComponentInfo(**c) for c in pa_raw.get("key_components", [])
                if isinstance(c, dict)
            ],
        )

        # Convert relative path keys → absolute using mapping built during serialization
        raw_folder_decisions = data.get("folders", {})
        folder_decisions: dict = {}
        rel_to_abs = getattr(self, "_rel_to_abs", {})
        for key, info in raw_folder_decisions.items():
            abs_path = (
                rel_to_abs.get(key)
                or rel_to_abs.get(key.rstrip("/"))
                or rel_to_abs.get(key + "/")
            )
            folder_decisions[abs_path or key] = info

        self._apply_folder_decisions(folder_decisions)

        if on_progress:
            excluded_count = sum(
                1 for v in folder_decisions.values()
                if isinstance(v, dict) and v.get("decision") == "excluded"
            )
            kept_count = sum(
                1 for v in folder_decisions.values()
                if isinstance(v, dict) and v.get("decision") == "keep"
            )
            await on_progress(
                f"Phase 2a complete: {kept_count} folders kept, "
                f"{excluded_count} folders excluded by LLM"
            )

        # Store folder decisions for later application
        self._folder_decisions = folder_decisions
        self._project_analysis = project_analysis

        return project_analysis

    def _apply_folder_decisions(self, decisions: dict):
        """Store folder decisions — they'll be applied when trees are available."""
        self._pending_folder_decisions = decisions

    def apply_folder_decisions_to_trees(self, trees: list[TreeNode]):
        """Apply stored folder decisions to tree nodes."""
        decisions = getattr(self, "_pending_folder_decisions", {})
        if not decisions:
            return

        for tree in trees:
            self._apply_to_node(tree, decisions)

    def _apply_to_node(self, node: TreeNode, decisions: dict):
        """Recursively apply folder decisions to a tree node."""
        if node.is_file:
            return

        info = decisions.get(node.path, {})
        if not isinstance(info, dict):
            return

        decision = info.get("decision", "")
        if decision == "excluded":
            node.decision = "excluded"
            node.phase2_reason = info.get("reason", "LLM excluded folder")
            node.description = info.get("purpose", "")
            # Cascade exclusion to all children
            self._cascade_exclusion(node, info.get("reason", "parent folder excluded"))
        elif decision == "keep":
            node.decision = "keep"
            node.description = info.get("purpose", "")
            node.technologies = info.get("technologies", [])
            node.importance = info.get("importance", "")

        for child in node.children:
            if not child.is_file and child.decision != "excluded":
                self._apply_to_node(child, decisions)

    def _cascade_exclusion(self, node: TreeNode, reason: str):
        """Mark all descendants as excluded."""
        for child in node.children:
            child.decision = "excluded"
            child.phase2_reason = reason
            if child.content_type == "":
                child.content_type = "excluded"
            if not child.is_file:
                self._cascade_exclusion(child, reason)

    # ------------------------------------------------------------------
    # Step 2: File evaluation (in batches)
    # ------------------------------------------------------------------

    async def _step2_files(
        self,
        files: list[TreeNode],
        project_analysis: ProjectAnalysis,
        product_name: str,
        product_description: str,
        on_progress=None,
        on_debug_prompt: Optional[Callable[[str, str, str, Optional[int], Optional[int]], None]] = None,
    ):
        """Evaluate individual files with the LLM in batches."""
        total_batches = (len(files) + FILTER_BATCH_SIZE - 1) // FILTER_BATCH_SIZE

        for batch_idx in range(total_batches):
            start = batch_idx * FILTER_BATCH_SIZE
            batch = files[start : start + FILTER_BATCH_SIZE]

            if on_progress:
                await on_progress(
                    f"Phase 2b: LLM evaluating files batch {batch_idx + 1}/{total_batches} "
                    f"({len(batch)} files, {len(files) - start - len(batch)} remaining)…"
                )

            await self._evaluate_file_batch(
                batch, project_analysis, product_name, product_description,
                on_debug_prompt=on_debug_prompt,
                batch_idx=batch_idx,
                total_batches=total_batches,
            )

            if on_progress:
                kept = sum(1 for f in batch if f.decision == "keep")
                excluded = sum(1 for f in batch if f.decision == "excluded")
                await on_progress(
                    f"Phase 2b: Batch {batch_idx + 1}/{total_batches} done — "
                    f"{kept} kept, {excluded} excluded"
                )

    async def _evaluate_file_batch(
        self,
        files: list[TreeNode],
        project_analysis: ProjectAnalysis,
        product_name: str,
        product_description: str,
        on_debug_prompt: Optional[Callable[[str, str, str, Optional[int], Optional[int]], None]] = None,
        batch_idx: Optional[int] = None,
        total_batches: Optional[int] = None,
    ):
        """Send a batch of files to the LLM for evaluation."""
        abs_to_rel = getattr(self, "_abs_to_rel", {})
        rel_to_abs = getattr(self, "_rel_to_abs", {})

        # Build file list with relative paths
        file_lines: list[str] = []
        for f in files:
            rel_path = abs_to_rel.get(f.path, f.path)
            file_lines.append(json.dumps({
                "path": rel_path,
                "name": f.name,
                "ext": f.ext,
                "size_bytes": f.size_bytes,
                "modified_at": f.modified_at,
                "phase1": "excluded" if f.phase1_excluded else "active",
            }))

        # Collect excluded folders (relative) for context
        excluded_folders: list[str] = []
        pending = getattr(self, "_pending_folder_decisions", {})
        for abs_path, info in pending.items():
            if isinstance(info, dict) and info.get("decision") == "excluded":
                rel = abs_to_rel.get(abs_path, abs_path)
                reason = info.get("reason", "")
                excluded_folders.append(f"  {rel}  — {reason}" if reason else f"  {rel}")

        context = (
            f"=== PROJECT CONTEXT ===\n"
            f"Product: {product_name}\n"
            f"Description: {product_description or '(none)'}\n"
            f"Project summary: {project_analysis.summary}\n"
            f"Technologies: {', '.join(project_analysis.technologies)}\n"
            f"Architecture: {project_analysis.architecture_style}\n\n"
        )
        if excluded_folders:
            context += (
                f"=== EXCLUDED FOLDERS (already removed, {len(excluded_folders)} folders) ===\n"
                + "\n".join(excluded_folders) + "\n\n"
            )
        context += (
            f"=== FILES TO EVALUATE ({len(files)} files — only from kept folders) ===\n"
            + "\n".join(file_lines)
        )

        if on_debug_prompt:
            on_debug_prompt("file", _FILE_SYSTEM_PROMPT, context, batch_idx, total_batches)

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _FILE_SYSTEM_PROMPT},
                    {"role": "user", "content": context},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)
        except Exception as exc:
            logger.error("phase2_file_llm_failed", error=str(exc), batch_size=len(files))
            for f in files:
                if f.phase1_excluded:
                    f.decision = "excluded"
                    f.content_type = "excluded"
                else:
                    f.decision = "keep"
                    f.content_type = self._guess_content_type(f.ext)
            return

        file_decisions = data.get("files", data)
        if not isinstance(file_decisions, dict):
            file_decisions = {}

        # Build lookup by relative path → node
        rel_to_node: dict[str, TreeNode] = {}
        for f in files:
            rel = abs_to_rel.get(f.path, f.path)
            rel_to_node[rel] = f

        for path, info in file_decisions.items():
            node = rel_to_node.get(path)
            if not node or not isinstance(info, dict):
                continue

            node.decision = info.get("decision", "keep")
            node.content_type = info.get("content_type", self._guess_content_type(node.ext))
            node.description = info.get("description", "")
            node.component = info.get("component", "")
            node.role = info.get("role", "")
            node.relationships = info.get("relationships", [])
            node.importance = info.get("importance", "medium")
            node.phase2_reason = info.get("reason", "")

        # Any files not in the LLM response — apply defaults
        for f in files:
            if not f.decision:
                if f.phase1_excluded:
                    f.decision = "excluded"
                    f.content_type = "excluded"
                else:
                    f.decision = "keep"
                    f.content_type = self._guess_content_type(f.ext)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _collect_surviving_files(self, node: TreeNode) -> list[TreeNode]:
        """Collect files that survived folder-level exclusion."""
        if node.is_file:
            # Include Phase 1 excluded files too so LLM can echo them
            if node.decision != "excluded":
                return [node]
            return []
        if node.decision == "excluded":
            return []
        result: list[TreeNode] = []
        for child in node.children:
            result.extend(self._collect_surviving_files(child))
        return result

    def _build_path_mappings(self, trees: list[TreeNode]) -> None:
        """Build _rel_to_abs and _abs_to_rel for every node (folders + files).

        Required for step 2 file evaluation.  Call before _serialize_folder_summary
        so folder step and file step both have correct path lookups.
        """
        self._rel_to_abs = {}
        self._abs_to_rel = {}
        multi = len(trees) > 1

        for tree in trees:
            root_prefix = tree.path
            root_display = f"/{tree.name}" if multi else ""
            self._add_node_to_mappings(tree, root_prefix=root_prefix, root_display=root_display)

    def _add_node_to_mappings(self, node: TreeNode, root_prefix: str = "", root_display: str = "") -> None:
        """Recursively add one node and its children to path mappings."""
        if node.path == root_prefix:
            rel = root_display + "/"
        else:
            rel = root_display + node.path[len(root_prefix):]
        if not node.is_file and not rel.endswith("/"):
            rel = rel + "/"
        self._rel_to_abs[rel] = node.path
        self._abs_to_rel[node.path] = rel
        for child in node.children:
            self._add_node_to_mappings(child, root_prefix=root_prefix, root_display=root_display)

    def _serialize_folder_summary(self, trees: list[TreeNode]) -> str:
        """Serialize a folder-only summary: one line per folder with path, file count, size.

        Ensures the LLM sees every folder (venv, node_modules, logs, etc.) without
        truncation.  Requires _build_path_mappings(trees) to have been called.
        """
        lines: list[str] = []
        for tree in trees:
            for folder in tree.all_folders():
                rel = self._abs_to_rel.get(folder.path, folder.path)
                if not rel.endswith("/"):
                    rel = rel + "/"
                nfiles = folder.file_count()
                total_bytes = sum(f.size_bytes for f in folder.all_files())
                size_str = _human_size(total_bytes)
                tag = " [PHASE1_EXCLUDED]" if folder.phase1_excluded else ""
                lines.append(f"[FOLDER] {rel}  ({nfiles} files, {size_str}){tag}")
        return "\n".join(lines)

    def _serialize_trees(self, trees: list[TreeNode], max_chars: int = 80_000) -> str:
        """Serialize full trees (folders + files) for the LLM.  May truncate on large trees.

        Prefer _build_path_mappings + _serialize_folder_summary for step 1 so the LLM
        sees all folders.  This method is kept for compatibility or fallback.
        """
        lines: list[str] = []
        self._rel_to_abs = {}
        self._abs_to_rel = {}
        multi = len(trees) > 1

        for tree in trees:
            root_prefix = tree.path
            root_display = f"/{tree.name}" if multi else ""
            self._serialize_node(tree, lines, indent=0,
                                 root_prefix=root_prefix,
                                 root_display=root_display)

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n… (truncated)"
        return text

    def _serialize_node(self, node: TreeNode, lines: list[str], indent: int,
                        root_prefix: str = "", root_display: str = ""):
        prefix = "  " * indent

        # Compute relative path
        if node.path == root_prefix:
            rel = root_display + "/"
        else:
            rel = root_display + node.path[len(root_prefix):]

        # Store bidirectional mapping
        self._rel_to_abs[rel] = node.path
        self._abs_to_rel[node.path] = rel

        if node.is_file:
            tag = " [PHASE1_EXCLUDED]" if node.phase1_excluded else ""
            size = _human_size(node.size_bytes)
            lines.append(
                f"{prefix}[FILE] {node.name}  ({node.ext}, {size}, {node.modified_at[:10]}){tag}"
            )
        else:
            tag = " [PHASE1_EXCLUDED]" if node.phase1_excluded else ""
            folder_display = rel if rel.endswith("/") else rel + "/"
            lines.append(f"{prefix}[FOLDER] {folder_display}{tag}")
            for child in node.children:
                self._serialize_node(child, lines, indent + 1,
                                     root_prefix=root_prefix,
                                     root_display=root_display)

    @staticmethod
    def _guess_content_type(ext: str) -> str:
        """Fallback content_type guess when LLM doesn't respond."""
        ext = ext.lower()
        text_exts = {
            ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".cs", ".c", ".cpp",
            ".h", ".hpp", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
            ".sh", ".bash", ".zsh", ".ps1", ".md", ".markdown", ".rst", ".txt",
            ".html", ".htm", ".css", ".scss", ".json", ".yaml", ".yml", ".toml",
            ".ini", ".cfg", ".conf", ".xml", ".csv", ".tsv", ".sql", ".proto",
            ".tf", ".hcl", ".dockerfile", ".makefile", ".cmake", ".gradle",
            ".log", ".diff", ".patch", ".gitignore", ".editorconfig",
            ".cbl", ".cob", ".cpy", ".jcl", ".r", ".lua", ".ex", ".exs",
            ".clj", ".hs", ".dart", ".v", ".vhd", ".asm",
            ".bat", ".cmd", ".properties", ".env",
        }
        image_exts = {
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif",
            ".webp", ".ico", ".svg", ".svgz", ".heic", ".heif", ".avif",
            ".psd", ".ai", ".eps", ".drawio", ".raw", ".cr2", ".nef",
        }
        mixed_exts = {
            ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
            ".odt", ".ods", ".odp", ".vsdx", ".vsd", ".rtf", ".epub",
            ".ipynb", ".one", ".djvu",
        }
        if ext in text_exts:
            return "text"
        if ext in image_exts:
            return "image"
        if ext in mixed_exts:
            return "text_or_image"
        return "text"  # default to text for unknown


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f}{unit}" if unit != "B" else f"{nbytes}{unit}"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:.1f}TB"
