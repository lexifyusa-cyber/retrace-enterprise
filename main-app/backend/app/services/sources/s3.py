"""AWS S3 source connector (boto3)."""

from __future__ import annotations

import os
from typing import Any

from app.services.sources.base import SourceConnector, DirEntry, ConnectorError


class S3Connector(SourceConnector):
    source_type = "s3"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self._client = None

    def _connect(self):
        if self._client is not None:
            return self._client
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:
            raise ConnectorError(
                "S3 support requires boto3. Run: pip install boto3"
            ) from e

        access_key = self.config.get("access_key")
        secret_key = self.config.get("secret_key")
        region = self.config.get("region") or "us-east-1"
        endpoint_url = self.config.get("endpoint_url") or None

        if not access_key or not secret_key:
            raise ConnectorError("S3 config requires `access_key` and `secret_key`")

        try:
            self._client = boto3.client(
                "s3",
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
                endpoint_url=endpoint_url,
                config=Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 3}),
            )
        except Exception as e:
            raise ConnectorError(f"S3 client init failed: {e}") from e
        return self._client

    @property
    def _bucket(self) -> str:
        bucket = self.config.get("bucket")
        if not bucket:
            raise ConnectorError("S3 config requires `bucket`")
        return bucket

    def test(self) -> tuple[bool, str]:
        try:
            client = self._connect()
            # Cheapest reachability + auth check: head the bucket
            client.head_bucket(Bucket=self._bucket)
            return True, f"Bucket '{self._bucket}' reachable"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"S3 error: {e}"

    def list_dir(self, path: str = "/") -> list[DirEntry]:
        # S3 has no real directories; we emulate via prefix + delimiter.
        client = self._connect()
        prefix = path.lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        try:
            resp = client.list_objects_v2(
                Bucket=self._bucket,
                Prefix=prefix,
                Delimiter="/",
                MaxKeys=1000,
            )
        except Exception as e:
            raise ConnectorError(f"S3 list failed: {e}") from e

        entries: list[DirEntry] = []
        # Sub-prefixes (act as directories)
        for cp in resp.get("CommonPrefixes") or []:
            sub = cp["Prefix"][len(prefix):].rstrip("/")
            if sub:
                entries.append(DirEntry(
                    name=sub, path="/" + cp["Prefix"], is_dir=True, size=0,
                ))
        # Leaf objects
        for obj in resp.get("Contents") or []:
            key = obj["Key"]
            if key == prefix:
                continue  # skip the marker
            name = key[len(prefix):]
            if "/" in name:
                continue  # belongs to a deeper level
            entries.append(DirEntry(
                name=name, path="/" + key, is_dir=False, size=int(obj.get("Size") or 0),
            ))
        return sorted(entries, key=lambda e: (not e.is_dir, e.name))

    def download_to(self, path: str, dest_dir: str, recursive: bool = True) -> str:
        client = self._connect()
        os.makedirs(dest_dir, exist_ok=True)
        key_or_prefix = path.lstrip("/")
        # Treat as directory if it ends with / OR if listing under it returns content
        is_dir = key_or_prefix.endswith("/") or self._is_prefix(client, key_or_prefix)

        leaf = key_or_prefix.rstrip("/").split("/")[-1] or "root"
        target = os.path.join(dest_dir, leaf)

        if is_dir:
            os.makedirs(target, exist_ok=True)
            self._download_prefix(client, key_or_prefix.rstrip("/") + "/", target)
        else:
            try:
                client.download_file(self._bucket, key_or_prefix, target)
            except Exception as e:
                raise ConnectorError(f"S3 download failed for {key_or_prefix}: {e}") from e
        return target

    def _is_prefix(self, client, key: str) -> bool:
        if not key:
            return True
        prefix = key if key.endswith("/") else key + "/"
        resp = client.list_objects_v2(Bucket=self._bucket, Prefix=prefix, MaxKeys=1)
        return bool(resp.get("KeyCount", 0))

    def _download_prefix(self, client, prefix: str, local_dir: str):
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                rel = key[len(prefix):]
                if not rel or rel.endswith("/"):
                    continue
                dest = os.path.join(local_dir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                try:
                    client.download_file(self._bucket, key, dest)
                except Exception:
                    continue  # skip individual failures
