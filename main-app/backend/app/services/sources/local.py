"""Local filesystem connector (wraps the existing on-disk paths)."""

from __future__ import annotations

import os
import shutil
from typing import Any

from app.services.sources.base import SourceConnector, DirEntry, ConnectorError


class LocalConnector(SourceConnector):
    source_type = "local"

    def test(self) -> tuple[bool, str]:
        # Always reachable — the only failure mode is the path the caller
        # later passes not existing, which we'd surface from list_dir/download.
        return True, "Local filesystem"

    def list_dir(self, path: str = "/") -> list[DirEntry]:
        if not os.path.isdir(path):
            raise ConnectorError(f"Path is not a directory: {path}")
        try:
            entries: list[DirEntry] = []
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                try:
                    st = os.stat(full)
                    entries.append(DirEntry(
                        name=name,
                        path=full,
                        is_dir=os.path.isdir(full),
                        size=st.st_size if not os.path.isdir(full) else 0,
                    ))
                except OSError:
                    continue  # skip unreadable entries
            return entries
        except PermissionError as e:
            raise ConnectorError(f"Permission denied: {path}") from e

    def download_to(self, path: str, dest_dir: str, recursive: bool = True) -> str:
        # Local connector doesn't actually need to "download" — but for API
        # uniformity, copy the path into dest_dir so the rest of the pipeline
        # can treat it the same way.
        if not os.path.exists(path):
            raise ConnectorError(f"Path does not exist: {path}")
        os.makedirs(dest_dir, exist_ok=True)
        leaf = os.path.basename(path.rstrip("/")) or "root"
        target = os.path.join(dest_dir, leaf)
        if os.path.isdir(path):
            if recursive:
                shutil.copytree(path, target, dirs_exist_ok=True)
            else:
                os.makedirs(target, exist_ok=True)
        else:
            shutil.copy2(path, target)
        return target
