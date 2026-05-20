"""
FileClassifierService – LLM-driven file classification.

Sends team metadata (name, folder groups, types) and file metadata
(name, path, extension, size) to an LLM which decides the ProcessingType
for each file.  No file contents are sent.
"""

import json
from typing import Optional

import structlog
from openai import AsyncOpenAI


from app.rag.models import FileRecord, ClassifiedFile, ProcessingType

logger = structlog.get_logger()

# Maximum files per LLM classification batch (to stay within context limits)
# At ~50-80 tokens per file metadata, 1000 files ≈ 80K tokens — fits within 128K context models
CLASSIFICATION_BATCH_SIZE = 1000

SYSTEM_PROMPT = """\
You are a file-classification engine for a knowledge management system.

You will receive:
  1. **Team context** – the team name, description, and a list of
     folder groups with their names and declared content types.
  2. **Folder tree** – the nested directory structure showing all files with
     their sizes so you can see where each file sits in the project.
  3. **A batch of files** – each with an index, file name, path, extension, and size.

Your job:  for EVERY file in the batch, decide:
  (a) which ProcessingType applies
  (b) which sub_category applies
  (c) whether the file should be **embedded** (chunked + stored in the vector DB)

Allowed ProcessingType values (pick exactly one per file):
  • "code"              – source code, scripts, config-as-code
  • "doc"               – written documentation (markdown, pdf, docx, txt, html, rst…)
  • "ticket_export"     – exported tickets / incidents / issues (csv, json, xlsx…)
  • "doc_with_diagrams" – documents that embed diagrams or architecture images
  • "diagram_image"     – standalone image files that are diagrams, architecture drawings, or screenshots of systems
  • "other"             – anything that doesn't fit the above

Allowed sub_category values PER processing_type:
  • code:              "backend", "frontend", "api", "database", "tests", "scripts", "config-as-code", "library", "general"
  • doc:               "user-guide", "api-docs", "architecture", "runbook", "release-notes", "tutorial", "reference", "general"
  • ticket_export:     "incident", "bug", "feature-request", "change-request", "general"
  • diagram_image:     "architecture", "flow-diagram", "sequence", "network", "er-diagram", "ui-wireframe", "general"
  • doc_with_diagrams: "architecture", "design", "general"
  • other:             "general"

**embed** (true/false) guidelines:
  • embed=true for: core source code, important documentation, API specs, runbooks,
    architecture docs, meaningful scripts, config-as-code with logic.
  • embed=false for: empty or near-empty files (<50 bytes), __init__.py with only
    imports or empty, test files, log files, generated output, style/CSS files,
    lock files, .gitignore, requirements.txt, package.json (dependency lists),
    boilerplate config (setup.cfg, tsconfig.json), and any file unlikely to answer
    a user question about how the team works.
  • Use file size as a signal: very small files (<100 bytes) are often empty stubs.
  • Use the folder tree to understand context: files in test/ or logs/ → embed=false.

Classification guidelines:
  • Use the folder group's declared type as a strong hint.  If a group is named
    "Incident Exports" with type "tickets", the files inside are likely ticket_export.
  • Use file extensions as a secondary signal (e.g. .py → code, .pdf → doc).
  • Use the folder path structure to help determine sub_category.
    E.g. files under /tests/ → sub_category "tests", files under /api/ → "api".
  • For documentation, use the filename and path to determine if it's a user guide,
    API docs, architecture doc, runbook, etc.
  • Images (.png, .jpg, .svg, .drawio) inside a folder group typed "diagrams"
    should be "diagram_image".  Random images elsewhere may be "other".
  • If a doc-typed group contains PDFs in a folder named "architecture" or "design",
    consider labelling them "doc_with_diagrams".
  • When in doubt, prefer the folder group's declared type over the extension.
  • When in doubt about sub_category, use "general".
  • When in doubt about embed, default to true.

Respond with ONLY a JSON array of objects, one per file, in the same order:
[
  { "index": 0, "processing_type": "code", "sub_category": "backend", "embed": true },
  { "index": 1, "processing_type": "doc", "sub_category": "user-guide", "embed": true },
  { "index": 2, "processing_type": "code", "sub_category": "tests", "embed": false },
  ...
]

No explanations, no markdown fences, no extra keys.  ONLY the JSON array.
"""


class TeamContext:
    """Lightweight container for team + folder-group metadata."""

    def __init__(
        self,
        team_name: str,
        team_description: Optional[str] = None,
        folder_groups: Optional[list[dict]] = None,
    ):
        self.team_name = team_name
        self.team_description = team_description or ""
        self.folder_groups = folder_groups or []

    def to_prompt_section(self) -> str:
        lines = [
            f"Team: {self.team_name}",
        ]
        if self.team_description:
            lines.append(f"Description: {self.team_description}")
        if self.folder_groups:
            lines.append("Folder Groups:")
            for g in self.folder_groups:
                name = g.get("group_name", "?")
                gtype = g.get("group_type", "?")
                paths = g.get("folder_paths", [])
                path_strs = ", ".join(p if isinstance(p, str) else p.get("absolute_path", "?") for p in paths[:5])
                if len(paths) > 5:
                    path_strs += f" … (+{len(paths) - 5} more)"
                lines.append(f"  • {name}  (type: {gtype})  paths: [{path_strs}]")
        return "\n".join(lines)


class FileClassifierService:
    """Classify files by asking an LLM to look at team context + file metadata."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: Optional[str] = None,
    ):
        kw: dict = {"api_key": api_key}
        if base_url:
            kw["base_url"] = base_url
        self._client = AsyncOpenAI(**kw)
        self._model = model

    async def classify(
        self,
        files: list[FileRecord],
        team_context: TeamContext,
        folder_tree: Optional[dict] = None,
    ) -> list[ClassifiedFile]:
        """Classify all files using the LLM in batches."""
        if not files:
            return []

        all_classified: list[ClassifiedFile] = []

        for batch_start in range(0, len(files), CLASSIFICATION_BATCH_SIZE):
            batch = files[batch_start : batch_start + CLASSIFICATION_BATCH_SIZE]
            results = await self._classify_batch(batch, team_context, batch_start, folder_tree)
            all_classified.extend(results)

        # Log stats
        stats: dict[str, int] = {}
        embed_counts = {"embed_yes": 0, "embed_no": 0}
        for c in all_classified:
            stats[c.processing_type.value] = stats.get(c.processing_type.value, 0) + 1
            if c.embed:
                embed_counts["embed_yes"] += 1
            else:
                embed_counts["embed_no"] += 1
        logger.info("classification_complete", total=len(all_classified), stats=stats, **embed_counts)

        return all_classified

    async def _classify_batch(
        self,
        batch: list[FileRecord],
        ctx: TeamContext,
        global_offset: int,
        folder_tree: Optional[dict] = None,
    ) -> list[ClassifiedFile]:
        """Send one batch to the LLM and parse its response."""

        # Build the file listing
        file_lines: list[str] = []
        for i, f in enumerate(batch):
            file_lines.append(
                json.dumps({
                    "index": i,
                    "name": f.name,
                    "path": f.path,
                    "ext": f.ext,
                    "size_bytes": f.size_bytes,
                })
            )

        tree_section = ""
        if folder_tree:
            tree_text = _serialize_tree(folder_tree)
            if len(tree_text) > 15000:
                tree_text = tree_text[:15000] + "\n... (truncated)"
            tree_section = f"\n=== FOLDER TREE ===\n{tree_text}\n"

        user_message = (
            f"=== TEAM CONTEXT ===\n"
            f"{ctx.to_prompt_section()}\n"
            f"{tree_section}\n"
            f"=== FILES TO CLASSIFY ({len(batch)} files) ===\n"
            + "\n".join(file_lines)
        )

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )

            raw = response.choices[0].message.content or "[]"
            parsed = self._parse_response(raw, len(batch))

        except Exception as exc:
            logger.error(
                "llm_classification_failed",
                error=str(exc),
                batch_offset=global_offset,
                batch_size=len(batch),
            )
            parsed = [(_fallback_type(f), "general", _fallback_embed(f)) for f in batch]

        # Build ClassifiedFile objects
        classified: list[ClassifiedFile] = []
        for i, f in enumerate(batch):
            if i < len(parsed):
                ptype, subcat, embed = parsed[i]
            else:
                ptype, subcat, embed = _fallback_type(f), "general", _fallback_embed(f)
            classified.append(
                ClassifiedFile(**f.model_dump(), processing_type=ptype, sub_category=subcat, embed=embed)
            )

        return classified

    @staticmethod
    def _parse_response(raw: str, expected_count: int) -> list[tuple[ProcessingType, str, bool]]:
        """Parse the LLM JSON response into a list of (ProcessingType, sub_category, embed) tuples."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("llm_response_not_json", raw=raw[:500])
            return []

        if isinstance(data, dict):
            for key in ("results", "classifications", "files", "items", "data"):
                if key in data and isinstance(data[key], list):
                    data = data[key]
                    break
            else:
                logger.warning("llm_response_unexpected_dict", keys=list(data.keys()))
                return []

        if not isinstance(data, list):
            logger.warning("llm_response_not_list", type=type(data).__name__)
            return []

        valid_types = {t.value for t in ProcessingType}
        result: list[tuple[ProcessingType, str, bool]] = []

        sorted_items = sorted(data, key=lambda x: x.get("index", 0) if isinstance(x, dict) else 0)

        for item in sorted_items:
            if isinstance(item, dict):
                raw_type = item.get("processing_type", "other")
                raw_subcat = item.get("sub_category", "general")
                raw_embed = item.get("embed", True)
            elif isinstance(item, str):
                raw_type = item
                raw_subcat = "general"
                raw_embed = True
            else:
                raw_type = "other"
                raw_subcat = "general"
                raw_embed = True

            raw_type = str(raw_type).lower().strip()
            raw_subcat = str(raw_subcat).lower().strip() or "general"
            embed = bool(raw_embed) if not isinstance(raw_embed, str) else raw_embed.lower() == "true"

            if raw_type in valid_types:
                result.append((ProcessingType(raw_type), raw_subcat, embed))
            else:
                result.append((ProcessingType.OTHER, raw_subcat, embed))

        return result


# ---------------------------------------------------------------------------
# Fallback heuristic (used if the LLM call fails entirely)
# ---------------------------------------------------------------------------

_CODE_EXT = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx",
    ".java", ".go", ".rb", ".cs", ".cpp", ".c", ".h", ".hpp",
    ".rs", ".swift", ".kt", ".scala", ".php",
    ".sh", ".bash", ".zsh", ".r", ".sql",
}
_DOC_EXT = {".md", ".mdx", ".rst", ".txt", ".pdf", ".docx", ".html", ".htm"}
_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".drawio", ".webp", ".bmp"}


def _fallback_type(f: FileRecord) -> ProcessingType:
    ext = f.ext.lower()
    if ext in _CODE_EXT:
        return ProcessingType.CODE
    if ext in _DOC_EXT:
        return ProcessingType.DOC
    if ext in _IMG_EXT:
        return ProcessingType.DIAGRAM_IMAGE
    if ext in {".csv", ".xlsx", ".json"} and any(
        kw in f.path.lower()
        for kw in ("ticket", "jira", "incident", "issue")
    ):
        return ProcessingType.TICKET_EXPORT
    return ProcessingType.OTHER


_SKIP_EMBED_NAMES = {
    "__init__.py", "__init__.pyi", "setup.cfg", "setup.py", "pyproject.toml",
    "tsconfig.json", "package.json", "package-lock.json", ".gitignore",
    ".dockerignore", "Makefile", "Dockerfile", "docker-compose.yml",
    "requirements.txt", "Pipfile", "Pipfile.lock", "poetry.lock",
    "yarn.lock", "pnpm-lock.yaml",
}

_SKIP_EMBED_EXT = {".log", ".css", ".scss", ".less", ".lock"}


def _fallback_embed(f: FileRecord) -> bool:
    """Heuristic embed decision used when the LLM call fails."""
    if f.size_bytes < 50:
        return False
    if f.name in _SKIP_EMBED_NAMES:
        return False
    if f.ext.lower() in _SKIP_EMBED_EXT:
        return False
    path_lower = f.path.lower()
    if any(seg in path_lower for seg in ("/test/", "/tests/", "/test_", "test_")):
        return False
    if "/logs/" in path_lower:
        return False
    return True


def _serialize_tree(node: dict, indent: int = 0) -> str:
    """Serialize a folder tree dict into a readable indented text representation."""
    lines: list[str] = []
    prefix = "  " * indent
    name = node.get("name", "")
    if indent > 0:
        lines.append(f"{prefix}{name}/")
    for f in node.get("_files", []):
        lines.append(f"{prefix}  {f['name']}  ({f.get('size', '')})")
    for child in node.get("children", []):
        lines.append(_serialize_tree(child, indent + 1))
    return "\n".join(lines)
