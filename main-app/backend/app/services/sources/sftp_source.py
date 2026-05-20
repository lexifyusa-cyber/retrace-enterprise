"""SFTP source connector (paramiko)."""

from __future__ import annotations

import io
import os
import stat
from typing import Any, Optional

from app.services.sources.base import SourceConnector, DirEntry, ConnectorError


class SFTPConnector(SourceConnector):
    source_type = "sftp"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self._client = None
        self._sftp = None

    # ── client lifecycle ─────────────────────────────────────────────

    def _connect(self):
        if self._sftp is not None:
            return self._sftp
        try:
            import paramiko
        except ImportError as e:
            raise ConnectorError(
                "SFTP support requires paramiko. Run: pip install paramiko"
            ) from e

        host = self.config.get("host")
        port = int(self.config.get("port") or 22)
        user = self.config.get("username")
        password = self.config.get("password")
        private_key = self.config.get("private_key")

        if not host or not user:
            raise ConnectorError("SFTP config requires `host` and `username`")
        if not (password or private_key):
            raise ConnectorError("SFTP config requires `password` or `private_key`")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs: dict[str, Any] = {
            "hostname": host,
            "port": port,
            "username": user,
            "timeout": 10,
            "banner_timeout": 10,
            "auth_timeout": 10,
        }
        if private_key:
            try:
                pkey = paramiko.RSAKey.from_private_key(io.StringIO(private_key))
            except paramiko.SSHException:
                # try other key types
                try:
                    pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key))
                except paramiko.SSHException as e:
                    raise ConnectorError(f"Invalid private key: {e}") from e
            kwargs["pkey"] = pkey
        else:
            kwargs["password"] = password

        try:
            client.connect(**kwargs)
        except Exception as e:
            raise ConnectorError(f"SFTP connect failed: {e}") from e

        self._client = client
        self._sftp = client.open_sftp()
        return self._sftp

    def close(self) -> None:
        try:
            if self._sftp:
                self._sftp.close()
            if self._client:
                self._client.close()
        finally:
            self._sftp = None
            self._client = None

    # ── connector API ────────────────────────────────────────────────

    def test(self) -> tuple[bool, str]:
        try:
            sftp = self._connect()
            sftp.listdir(".")
            return True, "Connected"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"SFTP error: {e}"
        finally:
            self.close()

    def list_dir(self, path: str = ".") -> list[DirEntry]:
        sftp = self._connect()
        try:
            attrs = sftp.listdir_attr(path or ".")
        except IOError as e:
            raise ConnectorError(f"Cannot list {path}: {e}") from e
        entries: list[DirEntry] = []
        for a in sorted(attrs, key=lambda x: x.filename):
            is_dir = stat.S_ISDIR(a.st_mode or 0)
            full = path.rstrip("/") + "/" + a.filename if path not in ("", "/") else "/" + a.filename
            entries.append(DirEntry(
                name=a.filename,
                path=full,
                is_dir=is_dir,
                size=int(a.st_size or 0) if not is_dir else 0,
            ))
        return entries

    def download_to(self, path: str, dest_dir: str, recursive: bool = True) -> str:
        sftp = self._connect()
        os.makedirs(dest_dir, exist_ok=True)
        try:
            try:
                attrs = sftp.stat(path)
            except IOError as e:
                raise ConnectorError(f"Path not found: {path}") from e
            is_dir = stat.S_ISDIR(attrs.st_mode or 0)
            leaf = path.rstrip("/").split("/")[-1] or "root"
            target = os.path.join(dest_dir, leaf)
            if is_dir:
                self._download_dir(sftp, path, target, recursive=recursive)
            else:
                sftp.get(path, target)
            return target
        finally:
            # don't close — caller may call download_to multiple times
            pass

    def _download_dir(self, sftp, remote_dir: str, local_dir: str, recursive: bool):
        os.makedirs(local_dir, exist_ok=True)
        for a in sftp.listdir_attr(remote_dir):
            remote_child = remote_dir.rstrip("/") + "/" + a.filename
            local_child = os.path.join(local_dir, a.filename)
            if stat.S_ISDIR(a.st_mode or 0):
                if recursive:
                    self._download_dir(sftp, remote_child, local_child, recursive=True)
            else:
                try:
                    sftp.get(remote_child, local_child)
                except Exception:
                    # skip unreadable / oversized files; don't fail whole sync
                    continue
