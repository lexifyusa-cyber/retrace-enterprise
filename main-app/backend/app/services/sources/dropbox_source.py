"""Dropbox source connector — OAuth 2 (refresh-token flow)."""

from __future__ import annotations

import os
from typing import Any

from app.core.config import settings
from app.services.sources.base import SourceConnector, DirEntry, ConnectorError


class DropboxConnector(SourceConnector):
    source_type = "dropbox"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self._client = None

    # ── client lifecycle ─────────────────────────────────────────────

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from dropbox import Dropbox
        except ImportError as e:
            raise ConnectorError(
                "Dropbox support requires dropbox. Run: pip install dropbox"
            ) from e

        refresh_token = self.config.get("refresh_token")
        if not refresh_token:
            raise ConnectorError("Dropbox connection missing refresh_token (re-connect)")
        if not (settings.DROPBOX_CLIENT_ID and settings.DROPBOX_CLIENT_SECRET):
            raise ConnectorError("DROPBOX_CLIENT_ID / DROPBOX_CLIENT_SECRET not configured")

        # SDK auto-refreshes access tokens when given refresh_token + app_key/secret.
        self._client = Dropbox(
            oauth2_refresh_token=refresh_token,
            app_key=settings.DROPBOX_CLIENT_ID,
            app_secret=settings.DROPBOX_CLIENT_SECRET,
        )
        return self._client

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ── connector API ────────────────────────────────────────────────

    def test(self) -> tuple[bool, str]:
        try:
            from dropbox.exceptions import AuthError, ApiError
            client = self._get_client()
            account = client.users_get_current_account()
            email = getattr(account, "email", "?")
            return True, f"Connected as {email}"
        except ConnectorError as e:
            return False, str(e)
        except AuthError as e:
            return False, f"Dropbox auth failed (re-connect): {e}"
        except ApiError as e:
            return False, f"Dropbox API error: {e}"
        except Exception as e:
            return False, f"Dropbox error: {e}"
        finally:
            self.close()

    def list_dir(self, path: str = "/") -> list[DirEntry]:
        from dropbox.exceptions import ApiError
        from dropbox.files import FolderMetadata, FileMetadata

        client = self._get_client()
        # Dropbox uses "" for the root — translate "/" to "".
        rel = "" if path in ("", "/") else path.rstrip("/")

        entries: list[DirEntry] = []
        try:
            res = client.files_list_folder(rel)
            while True:
                for item in res.entries:
                    full = path.rstrip("/") + "/" + item.name if path != "/" else "/" + item.name
                    if isinstance(item, FolderMetadata):
                        entries.append(DirEntry(name=item.name, path=full, is_dir=True, size=0))
                    elif isinstance(item, FileMetadata):
                        entries.append(DirEntry(name=item.name, path=full, is_dir=False, size=int(item.size)))
                if not res.has_more:
                    break
                res = client.files_list_folder_continue(res.cursor)
        except ApiError as e:
            raise ConnectorError(f"Dropbox list failed: {e}") from e

        return sorted(entries, key=lambda e: (not e.is_dir, e.name))

    def download_to(self, path: str, dest_dir: str, recursive: bool = True) -> str:
        from dropbox.exceptions import ApiError
        from dropbox.files import FolderMetadata, FileMetadata

        client = self._get_client()
        os.makedirs(dest_dir, exist_ok=True)
        leaf = path.rstrip("/").split("/")[-1] or "root"
        target = os.path.join(dest_dir, leaf)
        rel = "" if path in ("", "/") else path.rstrip("/")

        try:
            meta = client.files_get_metadata(rel)
        except ApiError as e:
            raise ConnectorError(f"Dropbox metadata failed: {e}") from e

        if isinstance(meta, FolderMetadata):
            os.makedirs(target, exist_ok=True)
            self._download_dir(client, rel, target, recursive)
        elif isinstance(meta, FileMetadata):
            self._download_file(client, rel, target)
        else:
            raise ConnectorError(f"Unexpected Dropbox entry type for {path!r}")
        return target

    # ── helpers ──────────────────────────────────────────────────────

    def _download_dir(self, client, remote_rel: str, local_dir: str, recursive: bool):
        from dropbox.exceptions import ApiError
        from dropbox.files import FolderMetadata, FileMetadata

        try:
            res = client.files_list_folder(remote_rel)
            entries = list(res.entries)
            while res.has_more:
                res = client.files_list_folder_continue(res.cursor)
                entries.extend(res.entries)
        except ApiError as e:
            raise ConnectorError(f"Dropbox list failed: {e}") from e

        for item in entries:
            child_rel = remote_rel + "/" + item.name if remote_rel else "/" + item.name
            child_local = os.path.join(local_dir, item.name)
            if isinstance(item, FolderMetadata):
                if recursive:
                    os.makedirs(child_local, exist_ok=True)
                    self._download_dir(client, child_rel, child_local, True)
            elif isinstance(item, FileMetadata):
                try:
                    self._download_file(client, child_rel, child_local)
                except Exception:
                    continue

    def _download_file(self, client, remote_rel: str, local_path: str):
        from dropbox.exceptions import ApiError

        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        try:
            client.files_download_to_file(local_path, remote_rel)
        except ApiError as e:
            raise ConnectorError(f"Dropbox download failed for {remote_rel!r}: {e}") from e
