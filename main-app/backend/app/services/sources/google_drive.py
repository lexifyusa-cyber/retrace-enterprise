"""Google Drive source connector — OAuth 2 (refresh-token flow).

Google Drive's API uses file IDs rather than POSIX paths. To keep parity with
the other connectors we synthesise a path of the form "/<folder>/<file>" by
walking from the root. The folder ID we last resolved is cached in `config`
so list_dir / download_to don't re-walk the tree.

Google-native documents (Docs, Sheets, Slides) cannot be downloaded directly
— they must be exported. We map them to the most useful office equivalents
(.docx / .xlsx / .pptx) on download.
"""

from __future__ import annotations

import io
import os
from typing import Any

from app.core.config import settings
from app.services.sources.base import SourceConnector, DirEntry, ConnectorError


# Google-native MIME types and what we export them as. Files with other
# mimeTypes get downloaded with files().get_media() unchanged.
_EXPORT_MIME = {
    "application/vnd.google-apps.document":     ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet":  ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",       ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing":      ("image/png", ".png"),
    "application/vnd.google-apps.script":       ("application/vnd.google-apps.script+json", ".json"),
}

_FOLDER_MIME = "application/vnd.google-apps.folder"


class GoogleDriveConnector(SourceConnector):
    source_type = "google_drive"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self._service = None

    # ── client lifecycle ─────────────────────────────────────────────

    def _get_service(self):
        if self._service is not None:
            return self._service
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as e:
            raise ConnectorError(
                "Google Drive support requires google-api-python-client and "
                "google-auth-oauthlib. Run: pip install google-api-python-client "
                "google-auth-oauthlib"
            ) from e

        refresh_token = self.config.get("refresh_token")
        if not refresh_token:
            raise ConnectorError("Google Drive connection missing refresh_token (re-connect)")
        if not (settings.GOOGLE_DRIVE_CLIENT_ID and settings.GOOGLE_DRIVE_CLIENT_SECRET):
            raise ConnectorError(
                "GOOGLE_DRIVE_CLIENT_ID / GOOGLE_DRIVE_CLIENT_SECRET not configured"
            )

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=settings.GOOGLE_DRIVE_CLIENT_ID,
            client_secret=settings.GOOGLE_DRIVE_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
        )
        # cache_discovery=False silences the file-cache warning under server runtimes.
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def close(self):
        if self._service is not None:
            try:
                self._service.close()
            except Exception:
                pass
            self._service = None

    # ── path → folder-id resolution ──────────────────────────────────

    def _resolve_folder_id(self, path: str) -> str:
        """Walk the user's My Drive from root, resolving '/a/b/c' to a folder ID."""
        if path in ("", "/"):
            return "root"
        service = self._get_service()
        parent = "root"
        for segment in [p for p in path.strip("/").split("/") if p]:
            # Search direct children of `parent` for a folder named `segment`.
            q = (
                f"'{parent}' in parents and trashed = false "
                f"and mimeType = '{_FOLDER_MIME}' "
                f"and name = '{segment.replace(chr(39), chr(92) + chr(39))}'"
            )
            res = service.files().list(
                q=q, fields="files(id,name)", pageSize=2, supportsAllDrives=True
            ).execute()
            files = res.get("files", [])
            if not files:
                raise ConnectorError(f"Folder not found: {path!r}")
            parent = files[0]["id"]
        return parent

    # ── connector API ────────────────────────────────────────────────

    def test(self) -> tuple[bool, str]:
        try:
            service = self._get_service()
            about = service.about().get(fields="user(emailAddress)").execute()
            email = about.get("user", {}).get("emailAddress", "?")
            return True, f"Connected as {email}"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Google Drive error: {e}"
        finally:
            self.close()

    def list_dir(self, path: str = "/") -> list[DirEntry]:
        from googleapiclient.errors import HttpError

        service = self._get_service()
        try:
            folder_id = self._resolve_folder_id(path)
            entries: list[DirEntry] = []
            page_token = None
            base = path.rstrip("/") if path != "/" else ""
            while True:
                res = service.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id,name,mimeType,size)",
                    pageSize=200,
                    pageToken=page_token,
                ).execute()
                for item in res.get("files", []):
                    name = item["name"]
                    full = f"{base}/{name}"
                    is_dir = item["mimeType"] == _FOLDER_MIME
                    # Google-native docs return no size; everything else has bytes.
                    size = int(item.get("size") or 0) if not is_dir else 0
                    entries.append(DirEntry(name=name, path=full, is_dir=is_dir, size=size))
                page_token = res.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as e:
            raise ConnectorError(f"Google Drive list failed: {e}") from e

        return sorted(entries, key=lambda e: (not e.is_dir, e.name))

    def download_to(self, path: str, dest_dir: str, recursive: bool = True) -> str:
        from googleapiclient.errors import HttpError

        service = self._get_service()
        os.makedirs(dest_dir, exist_ok=True)
        leaf = path.rstrip("/").split("/")[-1] or "root"
        target = os.path.join(dest_dir, leaf)

        try:
            if path in ("", "/"):
                folder_id = "root"
                is_dir = True
                meta_name = leaf
                meta_mime = _FOLDER_MIME
            else:
                parent = self._resolve_folder_id("/".join(path.rstrip("/").split("/")[:-1]) or "/")
                name = leaf
                q = (
                    f"'{parent}' in parents and trashed = false "
                    f"and name = '{name.replace(chr(39), chr(92) + chr(39))}'"
                )
                res = service.files().list(
                    q=q, fields="files(id,name,mimeType,size)", pageSize=2
                ).execute()
                files = res.get("files", [])
                if not files:
                    raise ConnectorError(f"Not found: {path!r}")
                meta = files[0]
                folder_id = meta["id"]
                meta_name = meta["name"]
                meta_mime = meta["mimeType"]
                is_dir = meta_mime == _FOLDER_MIME
        except HttpError as e:
            raise ConnectorError(f"Google Drive metadata failed: {e}") from e

        if is_dir:
            os.makedirs(target, exist_ok=True)
            self._download_dir(service, folder_id, target, recursive)
        else:
            target = self._download_file(service, folder_id, meta_name, meta_mime, dest_dir)
        return target

    # ── helpers ──────────────────────────────────────────────────────

    def _download_dir(self, service, folder_id: str, local_dir: str, recursive: bool):
        from googleapiclient.errors import HttpError

        page_token = None
        try:
            while True:
                res = service.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id,name,mimeType,size)",
                    pageSize=200,
                    pageToken=page_token,
                ).execute()
                for item in res.get("files", []):
                    if item["mimeType"] == _FOLDER_MIME:
                        if recursive:
                            sub = os.path.join(local_dir, item["name"])
                            os.makedirs(sub, exist_ok=True)
                            self._download_dir(service, item["id"], sub, True)
                    else:
                        try:
                            self._download_file(service, item["id"], item["name"], item["mimeType"], local_dir)
                        except Exception:
                            continue
                page_token = res.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as e:
            raise ConnectorError(f"Google Drive list failed: {e}") from e

    def _download_file(self, service, file_id: str, name: str, mime: str, dest_dir: str) -> str:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload

        # Google-native docs need export; everything else uses get_media.
        if mime in _EXPORT_MIME:
            export_mime, ext = _EXPORT_MIME[mime]
            local_path = os.path.join(dest_dir, name + ext)
            request = service.files().export_media(fileId=file_id, mimeType=export_mime)
        else:
            local_path = os.path.join(dest_dir, name)
            request = service.files().get_media(fileId=file_id)

        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        buf = io.FileIO(local_path, "wb")
        try:
            downloader = MediaIoBaseDownload(buf, request, chunksize=4 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        except HttpError as e:
            buf.close()
            try:
                os.remove(local_path)
            except OSError:
                pass
            raise ConnectorError(f"Google Drive download failed for {name!r}: {e}") from e
        finally:
            try:
                buf.close()
            except Exception:
                pass
        return local_path
