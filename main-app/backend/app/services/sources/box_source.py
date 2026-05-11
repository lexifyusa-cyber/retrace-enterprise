"""Box source connector — OAuth 2 (refresh-token flow).

Box uses numeric folder/file IDs rather than POSIX paths. To keep parity with
the other connectors we synthesise a path of the form "/<folder>/<file>" by
walking from the root folder (id "0"). The Box SDK auto-refreshes the access
token on each call when given a refresh_token + client credentials.

Scope is configured at the app level in Box Developer Console (we use
"Read all files and folders stored in Box" — Box's analogue of read-only).
"""

from __future__ import annotations

import os
from typing import Any

from app.core.config import settings
from app.services.sources.base import SourceConnector, DirEntry, ConnectorError


class BoxConnector(SourceConnector):
    source_type = "box"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self._client = None
        self._oauth = None

    # ── client lifecycle ─────────────────────────────────────────────

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from boxsdk import OAuth2, Client
        except ImportError as e:
            raise ConnectorError(
                "Box support requires boxsdk. Run: pip install boxsdk"
            ) from e

        refresh_token = self.config.get("refresh_token")
        access_token = self.config.get("access_token", "")
        if not refresh_token:
            raise ConnectorError("Box connection missing refresh_token (re-connect)")
        if not (settings.BOX_CLIENT_ID and settings.BOX_CLIENT_SECRET):
            raise ConnectorError("BOX_CLIENT_ID / BOX_CLIENT_SECRET not configured")

        # Box SDK refreshes when the access_token is empty/expired. The tokens
        # callback persists the new pair so subsequent calls don't refresh again.
        def _on_tokens_refreshed(new_access, new_refresh):
            # Updated tokens are kept in-memory for the lifetime of this connector
            # instance. Caller is responsible for persisting via re-encryption if
            # they want the refresh_token rotation to survive process restarts.
            self.config["access_token"] = new_access
            self.config["refresh_token"] = new_refresh

        self._oauth = OAuth2(
            client_id=settings.BOX_CLIENT_ID,
            client_secret=settings.BOX_CLIENT_SECRET,
            access_token=access_token or None,
            refresh_token=refresh_token,
            store_tokens=_on_tokens_refreshed,
        )
        self._client = Client(self._oauth)
        return self._client

    def close(self):
        # boxsdk's Client doesn't hold persistent connections; nothing to release.
        self._client = None
        self._oauth = None

    # ── path → folder-id resolution ──────────────────────────────────

    def _resolve_folder_id(self, path: str) -> str:
        """Walk from root, resolving '/a/b/c' to a Box folder ID. Root is '0'."""
        if path in ("", "/"):
            return "0"
        client = self._get_client()
        parent = "0"
        for segment in [p for p in path.strip("/").split("/") if p]:
            match = None
            offset = 0
            while True:
                items = client.folder(parent).get_items(limit=1000, offset=offset, fields=["id", "name", "type"])
                count = 0
                for item in items:
                    count += 1
                    if item.type == "folder" and item.name == segment:
                        match = item.id
                        break
                if match or count < 1000:
                    break
                offset += 1000
            if match is None:
                raise ConnectorError(f"Folder not found: {path!r}")
            parent = match
        return parent

    # ── connector API ────────────────────────────────────────────────

    def test(self) -> tuple[bool, str]:
        try:
            from boxsdk.exception import BoxAPIException, BoxOAuthException
            client = self._get_client()
            user = client.user().get()
            login = getattr(user, "login", None) or getattr(user, "name", "?")
            return True, f"Connected as {login}"
        except ConnectorError as e:
            return False, str(e)
        except BoxOAuthException as e:
            return False, f"Box auth failed (re-connect): {e}"
        except BoxAPIException as e:
            return False, f"Box API error: {e}"
        except Exception as e:
            return False, f"Box error: {e}"
        finally:
            self.close()

    def list_dir(self, path: str = "/") -> list[DirEntry]:
        from boxsdk.exception import BoxAPIException

        client = self._get_client()
        base = path.rstrip("/") if path != "/" else ""
        try:
            folder_id = self._resolve_folder_id(path)
            entries: list[DirEntry] = []
            offset = 0
            while True:
                items = list(client.folder(folder_id).get_items(
                    limit=1000, offset=offset, fields=["id", "name", "type", "size"]
                ))
                for item in items:
                    is_dir = item.type == "folder"
                    size = int(getattr(item, "size", 0) or 0) if not is_dir else 0
                    entries.append(DirEntry(
                        name=item.name,
                        path=f"{base}/{item.name}",
                        is_dir=is_dir,
                        size=size,
                    ))
                if len(items) < 1000:
                    break
                offset += 1000
        except BoxAPIException as e:
            raise ConnectorError(f"Box list failed: {e}") from e

        return sorted(entries, key=lambda e: (not e.is_dir, e.name))

    def download_to(self, path: str, dest_dir: str, recursive: bool = True) -> str:
        from boxsdk.exception import BoxAPIException

        client = self._get_client()
        os.makedirs(dest_dir, exist_ok=True)
        leaf = path.rstrip("/").split("/")[-1] or "root"
        target = os.path.join(dest_dir, leaf)

        try:
            if path in ("", "/"):
                # Root: treat as a directory download
                os.makedirs(target, exist_ok=True)
                self._download_folder(client, "0", target, recursive)
                return target

            parent_path = "/".join(path.rstrip("/").split("/")[:-1]) or "/"
            parent_id = self._resolve_folder_id(parent_path)
            name = leaf

            # Find the leaf inside parent
            match = None
            offset = 0
            while True:
                items = list(client.folder(parent_id).get_items(
                    limit=1000, offset=offset, fields=["id", "name", "type", "size"]
                ))
                for item in items:
                    if item.name == name:
                        match = item
                        break
                if match or len(items) < 1000:
                    break
                offset += 1000

            if match is None:
                raise ConnectorError(f"Not found: {path!r}")

            if match.type == "folder":
                os.makedirs(target, exist_ok=True)
                self._download_folder(client, match.id, target, recursive)
            else:
                self._download_file(client, match.id, target)
        except BoxAPIException as e:
            raise ConnectorError(f"Box metadata failed: {e}") from e

        return target

    # ── helpers ──────────────────────────────────────────────────────

    def _download_folder(self, client, folder_id: str, local_dir: str, recursive: bool):
        from boxsdk.exception import BoxAPIException

        try:
            offset = 0
            while True:
                items = list(client.folder(folder_id).get_items(
                    limit=1000, offset=offset, fields=["id", "name", "type"]
                ))
                for item in items:
                    child_local = os.path.join(local_dir, item.name)
                    if item.type == "folder":
                        if recursive:
                            os.makedirs(child_local, exist_ok=True)
                            self._download_folder(client, item.id, child_local, True)
                    else:
                        try:
                            self._download_file(client, item.id, child_local)
                        except Exception:
                            continue
                if len(items) < 1000:
                    break
                offset += 1000
        except BoxAPIException as e:
            raise ConnectorError(f"Box folder list failed: {e}") from e

    def _download_file(self, client, file_id: str, local_path: str):
        from boxsdk.exception import BoxAPIException

        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        try:
            with open(local_path, "wb") as f:
                client.file(file_id).download_to(f)
        except BoxAPIException as e:
            raise ConnectorError(f"Box download failed for file {file_id!r}: {e}") from e
