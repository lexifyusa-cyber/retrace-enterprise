"""Safe artifact access and optional encryption for TeamKnowParse outputs."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

ENVELOPE_MARKER = "retrace.teamknowparse.encrypted.v1"
ALLOWED_ROOT_FILES = {"folder.json", "chunks.jsonl", "run_log.json", "validation_report.json"}


def _key_bytes() -> bytes | None:
    raw = (settings.TEAMKNOWPARSE_ARTIFACT_ENCRYPTION_KEY or "").strip()
    if not raw and settings.TEAMKNOWPARSE_ARTIFACT_ENCRYPTION_KEY_FILE:
        key_path = Path(settings.TEAMKNOWPARSE_ARTIFACT_ENCRYPTION_KEY_FILE).expanduser()
        if key_path.is_file():
            raw = key_path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass
    encoded = raw.encode("utf-8")
    if len(encoded) == 32:
        return encoded
    raise ValueError("TEAMKNOWPARSE_ARTIFACT_ENCRYPTION_KEY must be 32 bytes or base64-url encoded 32 bytes")


def encryption_available() -> bool:
    return bool(settings.TEAMKNOWPARSE_ARTIFACT_ENCRYPTION_ENABLED and _key_bytes())


def is_allowed_artifact_path(relative_path: str) -> bool:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        return False
    if len(path.parts) == 1:
        return path.name in ALLOWED_ROOT_FILES
    return len(path.parts) == 2 and path.parts[0] == "files" and path.name.endswith(".json")


def _artifact_files(run_dir: Path) -> list[Path]:
    files = [run_dir / name for name in sorted(ALLOWED_ROOT_FILES)]
    files.extend(sorted((run_dir / "files").glob("*.json")) if (run_dir / "files").is_dir() else [])
    return [path for path in files if path.is_file()]


def _is_encrypted(payload: bytes) -> bool:
    return payload.lstrip().startswith(b"{") and ENVELOPE_MARKER.encode("utf-8") in payload[:240]


def list_artifacts(run_dir: Path) -> list[dict[str, Any]]:
    artifacts = []
    for path in _artifact_files(run_dir):
        rel = path.relative_to(run_dir).as_posix()
        data = path.read_bytes()[:240]
        artifacts.append({
            "path": rel,
            "name": path.name,
            "size": path.stat().st_size,
            "encrypted": _is_encrypted(data),
            "content_type": "application/x-ndjson" if path.name.endswith(".jsonl") else "application/json",
        })
    return artifacts


def _encrypt_bytes(data: bytes, associated_data: bytes) -> bytes:
    key = _key_bytes()
    if not key:
        raise ValueError("artifact encryption key is not configured")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, data, associated_data)
    envelope = {
        "marker": ENVELOPE_MARKER,
        "alg": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def _decrypt_envelope(payload: bytes, associated_data: bytes) -> bytes:
    envelope = json.loads(payload.decode("utf-8"))
    if envelope.get("marker") != ENVELOPE_MARKER:
        return payload
    key = _key_bytes()
    if not key:
        raise ValueError("artifact is encrypted but TEAMKNOWPARSE_ARTIFACT_ENCRYPTION_KEY is not configured")
    nonce = base64.b64decode(envelope["nonce"])
    ciphertext = base64.b64decode(envelope["ciphertext"])
    return AESGCM(key).decrypt(nonce, ciphertext, associated_data)


def read_artifact(run_dir: Path, relative_path: str) -> bytes:
    if not is_allowed_artifact_path(relative_path):
        raise ValueError("artifact path is not allowed")
    run_root = run_dir.resolve()
    path = (run_root / relative_path).resolve()
    if not path.is_relative_to(run_root) or not path.is_file():
        raise FileNotFoundError(relative_path)
    payload = path.read_bytes()
    rel = path.relative_to(run_root).as_posix().encode("utf-8")
    if _is_encrypted(payload):
        return _decrypt_envelope(payload, rel)
    return payload


def encrypt_run_artifacts(run_dir: Path) -> dict[str, Any]:
    if not settings.TEAMKNOWPARSE_ARTIFACT_ENCRYPTION_ENABLED:
        return {"enabled": False, "encrypted": 0, "skipped": "disabled"}
    _key_bytes()
    run_root = run_dir.resolve()
    encrypted = 0
    for path in _artifact_files(run_root):
        payload = path.read_bytes()
        if _is_encrypted(payload):
            continue
        rel = path.relative_to(run_root).as_posix()
        path.write_bytes(_encrypt_bytes(payload, rel.encode("utf-8")))
        encrypted += 1
    return {"enabled": True, "encrypted": encrypted}
