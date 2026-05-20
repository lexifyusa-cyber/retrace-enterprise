"""SMB / CIFS source connector (smbprotocol low-level API)."""

from __future__ import annotations

import os
import stat as stat_mod
import uuid
from typing import Any

from app.services.sources.base import SourceConnector, DirEntry, ConnectorError


class SMBConnector(SourceConnector):
    source_type = "smb"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self._connection = None
        self._session = None
        self._tree = None

    # ── client lifecycle ─────────────────────────────────────────────

    def _connect(self):
        """Open a TCP connection + SMB session + tree connect, honouring the
        configured port. Cached for the lifetime of this connector instance."""
        if self._tree is not None:
            return self._tree
        try:
            from smbprotocol.connection import Connection
            from smbprotocol.session import Session
            from smbprotocol.tree import TreeConnect
        except ImportError as e:
            raise ConnectorError(
                "SMB support requires smbprotocol. Run: pip install smbprotocol"
            ) from e

        server = self.config.get("server")
        port = int(self.config.get("port") or 445)
        share = self.config.get("share")
        username = self.config.get("username")
        password = self.config.get("password")

        if not (server and share and username and password):
            raise ConnectorError("SMB config requires `server`, `share`, `username`, `password`")

        try:
            connection = Connection(uuid.uuid4(), server, port)
            connection.connect(timeout=10)
            session = Session(connection, username, password, require_encryption=False)
            session.connect()
            tree = TreeConnect(session, fr"\\{server}\{share}")
            tree.connect()
        except Exception as e:
            raise ConnectorError(f"SMB connect failed: {e}") from e

        self._connection = connection
        self._session = session
        self._tree = tree
        return tree

    def close(self):
        try:
            if self._tree:
                self._tree.disconnect()
            if self._session:
                self._session.disconnect()
            if self._connection:
                self._connection.disconnect()
        finally:
            self._tree = None
            self._session = None
            self._connection = None

    # ── connector API ────────────────────────────────────────────────

    def test(self) -> tuple[bool, str]:
        try:
            self._connect()
            return True, "SMB share reachable"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"SMB error: {e}"
        finally:
            self.close()

    def list_dir(self, path: str = "/") -> list[DirEntry]:
        from smbprotocol.open import (
            Open, ImpersonationLevel, FilePipePrinterAccessMask,
            ShareAccess, CreateDisposition, CreateOptions, FileAttributes,
            DirectoryAccessMask,
        )
        from smbprotocol.file_info import FileInformationClass

        tree = self._connect()

        # Convert UNIX-style /a/b/c to the form smbprotocol Open wants:
        # an empty string for the share root, or "a\b\c" for sub-paths.
        rel = path.strip("/").replace("/", "\\")

        dir_open = Open(tree, rel)
        try:
            dir_open.create(
                impersonation_level=ImpersonationLevel.Impersonation,
                desired_access=DirectoryAccessMask.FILE_LIST_DIRECTORY
                | DirectoryAccessMask.FILE_READ_ATTRIBUTES,
                file_attributes=FileAttributes.FILE_ATTRIBUTE_DIRECTORY,
                share_access=ShareAccess.FILE_SHARE_READ,
                create_disposition=CreateDisposition.FILE_OPEN,
                create_options=CreateOptions.FILE_DIRECTORY_FILE,
            )
            entries: list[DirEntry] = []
            try:
                raw = dir_open.query_directory("*", FileInformationClass.FILE_FULL_DIRECTORY_INFORMATION)
            except Exception as e:
                raise ConnectorError(f"SMB list failed: {e}") from e

            for item in raw:
                name = item["file_name"].get_value().decode("utf-16-le")
                if name in (".", ".."):
                    continue
                attrs = item["file_attributes"].get_value()
                is_dir = bool(attrs & FileAttributes.FILE_ATTRIBUTE_DIRECTORY)
                size = int(item["end_of_file"].get_value()) if not is_dir else 0
                full = path.rstrip("/") + "/" + name if path not in ("", "/") else "/" + name
                entries.append(DirEntry(name=name, path=full, is_dir=is_dir, size=size))
            return sorted(entries, key=lambda e: (not e.is_dir, e.name))
        except ConnectorError:
            raise
        except Exception as e:
            raise ConnectorError(f"SMB list failed: {e}") from e
        finally:
            try:
                dir_open.close(False)
            except Exception:
                pass

    def download_to(self, path: str, dest_dir: str, recursive: bool = True) -> str:
        from smbprotocol.open import (
            Open, ImpersonationLevel, FilePipePrinterAccessMask,
            ShareAccess, CreateDisposition, CreateOptions, FileAttributes,
            DirectoryAccessMask,
        )

        tree = self._connect()
        os.makedirs(dest_dir, exist_ok=True)
        leaf = path.rstrip("/").split("/")[-1] or "root"
        target = os.path.join(dest_dir, leaf)
        rel = path.strip("/").replace("/", "\\")

        # Determine if the path is a directory or file.
        probe = Open(tree, rel)
        try:
            probe.create(
                impersonation_level=ImpersonationLevel.Impersonation,
                desired_access=FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
                file_attributes=0,
                share_access=ShareAccess.FILE_SHARE_READ,
                create_disposition=CreateDisposition.FILE_OPEN,
                create_options=0,
            )
            attrs = probe.file_attributes
            is_dir = bool(attrs & FileAttributes.FILE_ATTRIBUTE_DIRECTORY)
        except Exception as e:
            raise ConnectorError(f"SMB stat failed: {e}") from e
        finally:
            try:
                probe.close(False)
            except Exception:
                pass

        if is_dir:
            os.makedirs(target, exist_ok=True)
            self._download_dir(tree, rel, target, recursive)
        else:
            self._download_file(tree, rel, target)
        return target

    # ── helpers ──────────────────────────────────────────────────────

    def _download_dir(self, tree, remote_rel: str, local_dir: str, recursive: bool):
        from smbprotocol.open import (
            Open, ImpersonationLevel, ShareAccess, CreateDisposition,
            CreateOptions, FileAttributes, DirectoryAccessMask,
        )
        from smbprotocol.file_info import FileInformationClass

        dir_open = Open(tree, remote_rel)
        try:
            dir_open.create(
                impersonation_level=ImpersonationLevel.Impersonation,
                desired_access=DirectoryAccessMask.FILE_LIST_DIRECTORY
                | DirectoryAccessMask.FILE_READ_ATTRIBUTES,
                file_attributes=FileAttributes.FILE_ATTRIBUTE_DIRECTORY,
                share_access=ShareAccess.FILE_SHARE_READ,
                create_disposition=CreateDisposition.FILE_OPEN,
                create_options=CreateOptions.FILE_DIRECTORY_FILE,
            )
            children = dir_open.query_directory("*", FileInformationClass.FILE_FULL_DIRECTORY_INFORMATION)
        finally:
            try:
                dir_open.close(False)
            except Exception:
                pass

        for item in children:
            name = item["file_name"].get_value().decode("utf-16-le")
            if name in (".", ".."):
                continue
            child_rel = remote_rel + "\\" + name if remote_rel else name
            child_local = os.path.join(local_dir, name)
            attrs = item["file_attributes"].get_value()
            if attrs & FileAttributes.FILE_ATTRIBUTE_DIRECTORY:
                if recursive:
                    os.makedirs(child_local, exist_ok=True)
                    self._download_dir(tree, child_rel, child_local, True)
            else:
                try:
                    self._download_file(tree, child_rel, child_local)
                except Exception:
                    continue  # skip individual files that error

    def _download_file(self, tree, remote_rel: str, local_path: str):
        from smbprotocol.open import (
            Open, ImpersonationLevel, FilePipePrinterAccessMask,
            ShareAccess, CreateDisposition, CreateOptions,
        )

        f = Open(tree, remote_rel)
        try:
            f.create(
                impersonation_level=ImpersonationLevel.Impersonation,
                desired_access=FilePipePrinterAccessMask.GENERIC_READ,
                file_attributes=0,
                share_access=ShareAccess.FILE_SHARE_READ,
                create_disposition=CreateDisposition.FILE_OPEN,
                create_options=CreateOptions.FILE_NON_DIRECTORY_FILE,
            )
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            offset = 0
            chunk = 1024 * 1024  # 1 MiB
            with open(local_path, "wb") as out:
                while True:
                    data = f.read(offset, chunk)
                    if not data:
                        break
                    out.write(data)
                    offset += len(data)
                    if len(data) < chunk:
                        break
        finally:
            try:
                f.close(False)
            except Exception:
                pass
