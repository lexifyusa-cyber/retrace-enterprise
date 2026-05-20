"""
Training-data source connectors.

Each connector implements `SourceConnector` and provides:
- `test()` — quick credential check
- `list_dir(path)` — list children of a directory
- `download_to(path, dest_dir)` — copy a file or recursive subtree to local disk
- `stat(path)` — metadata for a single entry

The training pipeline calls connectors via `get_connector(connection)` to
stage remote content into a local temp dir, then runs the existing
filesystem-based scanner against that dir.

Credential storage is JSON encrypted with the chunk key.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.rag.encryption import encrypt_text, decrypt_text


def encrypt_config(config: dict[str, Any]) -> str:
    """Serialise + encrypt a config dict. Returns base64 string."""
    blob = json.dumps(config, separators=(",", ":")).encode("utf-8")
    enc = encrypt_text(blob.decode("utf-8"))
    # encrypt_text returns bytes; store as base64 utf-8 string in TEXT column
    import base64
    return base64.b64encode(enc).decode("ascii")


def decrypt_config(encrypted: str) -> dict[str, Any]:
    """Reverse of encrypt_config."""
    import base64
    enc = base64.b64decode(encrypted.encode("ascii"))
    plain = decrypt_text(enc)
    return json.loads(plain)


# Lazy-import connector classes so importing this package doesn't pull in
# boto3/azure/paramiko unless a caller actually needs them.

def get_connector(source_type: str, config: dict[str, Any]):
    """Factory: returns a SourceConnector for the given source_type + config."""
    if source_type == "local":
        from app.services.sources.local import LocalConnector
        return LocalConnector(config)
    if source_type == "sftp":
        from app.services.sources.sftp_source import SFTPConnector
        return SFTPConnector(config)
    if source_type == "smb":
        from app.services.sources.smb_source import SMBConnector
        return SMBConnector(config)
    if source_type == "s3":
        from app.services.sources.s3 import S3Connector
        return S3Connector(config)
    if source_type == "azure_blob":
        from app.services.sources.azure_blob import AzureBlobConnector
        return AzureBlobConnector(config)
    if source_type == "dropbox":
        from app.services.sources.dropbox_source import DropboxConnector
        return DropboxConnector(config)
    if source_type == "google_drive":
        from app.services.sources.google_drive import GoogleDriveConnector
        return GoogleDriveConnector(config)
    if source_type == "box":
        from app.services.sources.box_source import BoxConnector
        return BoxConnector(config)
    raise ValueError(f"Unknown source_type: {source_type!r}")


SUPPORTED_SOURCE_TYPES = ("local", "sftp", "smb", "s3", "azure_blob", "dropbox", "google_drive", "box")


# Per-type config schema — used by the API to validate create/update payloads
# and by the frontend to render the right form. Each entry is a list of fields:
#   (name, label, type, required, secret)
# `type` is "string" | "int" | "password"
# `secret` means "do not echo in to_dict(include_config=True)"
SOURCE_CONFIG_SCHEMA: dict[str, list[dict[str, Any]]] = {
    "sftp": [
        {"name": "host",       "label": "Host",         "type": "string",   "required": True,  "secret": False},
        {"name": "port",       "label": "Port",         "type": "int",      "required": False, "secret": False, "default": 22},
        {"name": "username",   "label": "Username",     "type": "string",   "required": True,  "secret": False},
        {"name": "password",   "label": "Password",     "type": "password", "required": False, "secret": True},
        {"name": "private_key","label": "Private Key (PEM, alternative to password)", "type": "password", "required": False, "secret": True},
    ],
    "smb": [
        {"name": "server",     "label": "Server",       "type": "string",   "required": True,  "secret": False},
        {"name": "port",       "label": "Port",         "type": "int",      "required": False, "secret": False, "default": 445},
        {"name": "share",      "label": "Share Name",   "type": "string",   "required": True,  "secret": False},
        {"name": "domain",     "label": "Domain",       "type": "string",   "required": False, "secret": False, "default": ""},
        {"name": "username",   "label": "Username",     "type": "string",   "required": True,  "secret": False},
        {"name": "password",   "label": "Password",     "type": "password", "required": True,  "secret": True},
    ],
    "s3": [
        {"name": "region",     "label": "Region",       "type": "string",   "required": True,  "secret": False, "default": "us-east-1"},
        {"name": "access_key", "label": "Access Key ID","type": "string",   "required": True,  "secret": False},
        {"name": "secret_key", "label": "Secret Access Key", "type": "password", "required": True,  "secret": True},
        {"name": "bucket",     "label": "Bucket",       "type": "string",   "required": True,  "secret": False},
        {"name": "endpoint_url", "label": "Custom Endpoint (S3-compatible, optional)", "type": "string", "required": False, "secret": False},
    ],
    "azure_blob": [
        {"name": "account_name", "label": "Storage Account Name", "type": "string",   "required": True,  "secret": False},
        {"name": "account_key",  "label": "Account Key",          "type": "password", "required": True,  "secret": True},
        {"name": "container",    "label": "Container",            "type": "string",   "required": True,  "secret": False},
    ],
    # OAuth-based sources have no user-fillable fields. The frontend should
    # render a "Connect with …" button instead of a credentials form when
    # the schema is empty + the source is in OAUTH_SOURCE_TYPES.
    "dropbox": [],
    "google_drive": [],
    "box": [],
}


# Sources whose credentials come from an OAuth dance, not a form. The
# connections API exposes /oauth/init/<type> and /oauth/callback/<type>
# for these.
OAUTH_SOURCE_TYPES = ("dropbox", "google_drive", "box")
