"""
SourceConnector base class.

Each connector exposes a small, uniform API the training pipeline can call.
For remote sources, `download_to` stages content into a local temp directory
so the existing filesystem-based scanner / classifier can process it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class DirEntry:
    name: str
    path: str        # absolute path within the connection (e.g. "/foo/bar.txt", "s3://bucket/key")
    is_dir: bool
    size: int = 0    # bytes, 0 for directories


class SourceConnector(ABC):
    """Abstract base. Implementations live in sibling modules."""

    source_type: str = ""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    @abstractmethod
    def test(self) -> tuple[bool, str]:
        """Quick connectivity check. Returns (ok, message)."""

    @abstractmethod
    def list_dir(self, path: str = "/") -> list[DirEntry]:
        """List entries under `path`. Path semantics vary per connector
        (e.g. /tmp for SFTP/SMB, prefix for S3, container path for Azure)."""

    @abstractmethod
    def download_to(self, path: str, dest_dir: str, recursive: bool = True) -> str:
        """Stage `path` (file or directory) into `dest_dir`.

        Returns the absolute local path of the staged file or root directory.
        For a single file, that's `dest_dir/<basename(path)>`.
        For a directory, that's `dest_dir/<basename(path)>/...`.
        """

    def close(self) -> None:
        """Optional cleanup (close clients, etc.)."""


class ConnectorError(Exception):
    """Raised by connectors when an operation fails. Wrap upstream library
    errors with a useful message — never let raw paramiko/boto3 stack traces
    bubble into the API response."""
