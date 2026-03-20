"""
FolderCrawlerService — Phase 1 of the training pipeline.

Recursively walks directories and builds TreeNode trees.  During the walk
every file is checked against EXCLUDE_EXTENSIONS; matching files are tagged
``phase1_excluded=True``.  Folders whose **entire** subtree is excluded
bubble up to ``phase1_excluded`` as well.

Does not follow directory symlinks (avoids cycles and "file name too long").
Skips entries that raise ENAMETOOLONG or ELOOP.

Returns one TreeNode per root path (so multiple folder groups → multiple trees).
"""

import errno
from datetime import datetime
from pathlib import Path

import structlog

from app.rag.extension_registry import EXCLUDE_EXTENSIONS, IGNORE_DIRS
from app.rag.models import TreeNode

logger = structlog.get_logger()


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f}{unit}" if unit != "B" else f"{nbytes}{unit}"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:.1f}TB"


class FolderCrawlerService:
    """Build TreeNode trees from folder paths and apply Phase 1 exclusion."""

    def __init__(self):
        self.total_files = 0
        self.total_folders = 0
        self.excluded_count = 0
        self.kept_count = 0
        self.total_size_bytes = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def crawl(self, root_paths: list[str]) -> list[TreeNode]:
        """Walk *root_paths* and return one TreeNode per root.

        Each tree has Phase 1 exclusion tags already applied.
        """
        trees: list[TreeNode] = []
        seen: set[str] = set()

        for root in root_paths:
            rp = Path(root)
            if not rp.exists():
                logger.warning("crawl_path_missing", path=root)
                continue
            if not rp.is_dir():
                logger.warning("crawl_path_not_dir", path=root)
                continue
            tree = self._walk_dir(rp, seen)
            if tree:
                self._bubble_up_exclusion(tree)
                trees.append(tree)

        logger.info(
            "phase1_crawl_complete",
            roots=len(root_paths),
            trees=len(trees),
            total_files=self.total_files,
            total_folders=self.total_folders,
            excluded=self.excluded_count,
            kept=self.kept_count,
            total_size=_human_size(self.total_size_bytes),
        )
        return trees

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _walk_dir(self, directory: Path, seen: set[str]) -> TreeNode | None:
        """Recursively build a TreeNode for *directory*."""
        try:
            entries = sorted(directory.iterdir())
        except PermissionError:
            logger.warning("crawl_permission_denied", path=str(directory))
            return None
        except OSError as exc:
            if exc.errno in (errno.ENAMETOOLONG, errno.ELOOP):
                logger.warning("crawl_path_too_long_or_loop", path=str(directory), error=str(exc))
                return None
            raise

        node = TreeNode(
            name=directory.name,
            path=str(directory.resolve()),
            is_file=False,
        )
        self.total_folders += 1

        for entry in entries:
            # Skip ignore dirs and hidden entries
            if entry.name in IGNORE_DIRS or entry.name.startswith("."):
                continue

            # Do not follow directory symlinks (avoids cycles and ENAMETOOLONG)
            if entry.is_symlink():
                continue

            if entry.is_dir():
                child = self._walk_dir(entry, seen)
                if child and (child.children or child.is_file):
                    node.children.append(child)

            elif entry.is_file():
                try:
                    resolved = str(entry.resolve())
                except OSError as exc:
                    if exc.errno in (errno.ENAMETOOLONG, errno.ELOOP):
                        logger.warning("crawl_skip_path_too_long_or_loop", path=str(entry), error=str(exc))
                    else:
                        logger.warning("crawl_resolve_error", path=str(entry), error=str(exc))
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)

                try:
                    stat = entry.stat()
                except OSError as exc:
                    if exc.errno in (errno.ENAMETOOLONG, errno.ELOOP):
                        logger.warning("crawl_skip_path_too_long_or_loop", path=str(entry), error=str(exc))
                    else:
                        logger.warning("crawl_stat_error", path=str(entry), error=str(exc))
                    continue

                ext = entry.suffix.lower()
                size = stat.st_size
                self.total_files += 1
                self.total_size_bytes += size

                is_excluded = ext in EXCLUDE_EXTENSIONS
                # Also check compound extensions like .tar.gz, .min.js
                name_lower = entry.name.lower()
                if not is_excluded:
                    for skip_ext in EXCLUDE_EXTENSIONS:
                        if "." in skip_ext[1:] and name_lower.endswith(skip_ext):
                            is_excluded = True
                            break

                if is_excluded:
                    self.excluded_count += 1
                else:
                    self.kept_count += 1

                file_node = TreeNode(
                    name=entry.name,
                    path=resolved,
                    is_file=True,
                    ext=ext,
                    size_bytes=size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    phase1_excluded=is_excluded,
                    phase1_reason=f"binary extension '{ext}'" if is_excluded else "",
                )
                node.children.append(file_node)

        return node

    def _bubble_up_exclusion(self, node: TreeNode) -> bool:
        """Tag a folder as phase1_excluded if ALL descendant files are excluded.

        Returns True if the node (and its entire subtree) is excluded.
        """
        if node.is_file:
            return node.phase1_excluded

        if not node.children:
            return True  # empty folder — consider excluded

        all_excluded = all(self._bubble_up_exclusion(c) for c in node.children)
        if all_excluded:
            node.phase1_excluded = True
            node.phase1_reason = "all files in folder excluded"
        return all_excluded
