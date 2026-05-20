"""Azure Blob Storage source connector (azure-storage-blob)."""

from __future__ import annotations

import os
from typing import Any

from app.services.sources.base import SourceConnector, DirEntry, ConnectorError


class AzureBlobConnector(SourceConnector):
    source_type = "azure_blob"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self._service = None
        self._container_client = None

    def _connect(self):
        if self._container_client is not None:
            return self._container_client
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError as e:
            raise ConnectorError(
                "Azure Blob support requires azure-storage-blob. "
                "Run: pip install azure-storage-blob"
            ) from e

        account_name = self.config.get("account_name")
        account_key = self.config.get("account_key")
        container = self.config.get("container")
        if not (account_name and account_key and container):
            raise ConnectorError(
                "Azure Blob config requires `account_name`, `account_key`, and `container`"
            )

        url = f"https://{account_name}.blob.core.windows.net"
        try:
            self._service = BlobServiceClient(account_url=url, credential=account_key)
            self._container_client = self._service.get_container_client(container)
        except Exception as e:
            raise ConnectorError(f"Azure init failed: {e}") from e
        return self._container_client

    def test(self) -> tuple[bool, str]:
        try:
            client = self._connect()
            client.get_container_properties()
            return True, f"Container '{self.config.get('container')}' reachable"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Azure error: {e}"

    def list_dir(self, path: str = "/") -> list[DirEntry]:
        client = self._connect()
        prefix = path.lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        try:
            walker = client.walk_blobs(name_starts_with=prefix, delimiter="/")
        except Exception as e:
            raise ConnectorError(f"Azure list failed: {e}") from e

        entries: list[DirEntry] = []
        for item in walker:
            full = "/" + item.name
            name = item.name[len(prefix):].rstrip("/")
            if not name:
                continue
            # Walked entries can be either leaf blobs or virtual prefixes
            if hasattr(item, "size"):
                entries.append(DirEntry(name=name, path=full, is_dir=False, size=int(item.size or 0)))
            else:
                entries.append(DirEntry(name=name, path=full, is_dir=True, size=0))
        return sorted(entries, key=lambda e: (not e.is_dir, e.name))

    def download_to(self, path: str, dest_dir: str, recursive: bool = True) -> str:
        client = self._connect()
        os.makedirs(dest_dir, exist_ok=True)
        name = path.lstrip("/")
        leaf = name.rstrip("/").split("/")[-1] or "root"
        target = os.path.join(dest_dir, leaf)

        # Try as a single blob first; if that 404s, treat as a prefix.
        try:
            blob = client.get_blob_client(name)
            props = blob.get_blob_properties()
            with open(target, "wb") as f:
                blob.download_blob().readinto(f)
            return target
        except Exception:
            pass

        # Treat as prefix
        prefix = name if name.endswith("/") else name + "/"
        os.makedirs(target, exist_ok=True)
        try:
            for blob in client.list_blobs(name_starts_with=prefix):
                rel = blob.name[len(prefix):]
                if not rel:
                    continue
                dest = os.path.join(target, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                try:
                    bc = client.get_blob_client(blob.name)
                    with open(dest, "wb") as f:
                        bc.download_blob().readinto(f)
                except Exception:
                    continue
        except Exception as e:
            raise ConnectorError(f"Azure download failed: {e}") from e
        return target
