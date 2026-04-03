"""
Sandbox Manager — manages llm-sandbox sessions per conversation.

Each conversation gets a persistent Podman container via llm-sandbox.

Each conversation gets a persistent Podman container via llm-sandbox.
Files persist in ~/.retrace/sandboxes/{conversation_id}/ on the host,
mounted as /workspace inside the container.
"""

import os
import shutil
import time
from typing import Optional

import structlog

logger = structlog.get_logger()

# Active sessions keyed by conversation_id
_active_sessions: dict = {}
# Last activity timestamp per conversation_id
_session_activity: dict[str, float] = {}

IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes


def _patch_podman_exec(session):
    """Monkey-patch llm-sandbox's Podman container API to use CLI for exec.

    The Podman Python library's exec_run() returns empty stdout due to a
    demux bug. This patch replaces execute_command() with a version that
    calls `podman exec` via subprocess, which works correctly.
    """
    import subprocess as _sp

    original_api = session.container_api

    def patched_execute_command(container, command, **kwargs):
        cid = container.id[:12] if hasattr(container, 'id') else str(container)
        workdir = kwargs.get("workdir")

        cmd = ["podman", "exec"]
        if workdir:
            cmd.extend(["-w", workdir])
        cmd.append(cid)
        cmd.extend(["sh", "-c", command])

        try:
            proc = _sp.run(cmd, capture_output=True, text=True, timeout=120)
            # Return format expected by llm-sandbox: (exit_code, (stdout_bytes, stderr_bytes))
            stdout_bytes = proc.stdout.encode() if proc.stdout else b""
            stderr_bytes = proc.stderr.encode() if proc.stderr else b""
            return proc.returncode, (stdout_bytes, stderr_bytes)
        except _sp.TimeoutExpired:
            return 124, (b"", b"Command timed out after 120 seconds")
        except Exception as exc:
            return 1, (b"", str(exc).encode())

    original_api.execute_command = patched_execute_command

SANDBOX_BASE = os.path.expanduser("~/.retrace/sandboxes")
SANDBOX_IMAGE = "retrace-sandbox:latest"


def _check_podman_available() -> bool:
    """Check if podman is available on the system."""
    import subprocess
    try:
        result = subprocess.run(
            ["podman", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _check_image_exists() -> bool:
    """Check if the retrace-sandbox image is built."""
    import subprocess
    try:
        result = subprocess.run(
            ["podman", "image", "exists", SANDBOX_IMAGE],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _build_sandbox_image() -> bool:
    """Build the retrace-sandbox container image."""
    import subprocess
    dockerfile_dir = os.path.join(
        os.path.dirname(__file__),  # app/services/
        "..", "..", "..",           # main-app/backend/ -> main-app/
        "sandbox",
    )
    dockerfile_dir = os.path.abspath(dockerfile_dir)

    if not os.path.isfile(os.path.join(dockerfile_dir, "Dockerfile")):
        logger.error("sandbox_dockerfile_missing", path=dockerfile_dir)
        return False

    logger.info("sandbox_building_image", image=SANDBOX_IMAGE, context=dockerfile_dir)
    try:
        result = subprocess.run(
            ["podman", "build", "-t", SANDBOX_IMAGE, dockerfile_dir],
            capture_output=True,
            text=True,
            timeout=600,  # 10 minutes for build
        )
        if result.returncode == 0:
            logger.info("sandbox_image_built", image=SANDBOX_IMAGE)
            return True
        else:
            logger.error("sandbox_image_build_failed", stderr=result.stderr[:500])
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.error("sandbox_image_build_error", error=str(exc))
        return False


def is_sandbox_available() -> dict:
    """Check if sandboxing is available. Returns status dict."""
    podman_ok = _check_podman_available()
    if not podman_ok:
        return {
            "available": False,
            "reason": "Podman is not installed. Install it from https://podman.io/",
        }

    image_ok = _check_image_exists()
    if not image_ok:
        return {
            "available": False,
            "reason": "Sandbox image not built. Building automatically...",
            "needs_build": True,
        }

    return {"available": True}


def ensure_sandbox_ready() -> bool:
    """Ensure Podman is available and image is built. Returns True if ready."""
    if not _check_podman_available():
        logger.warning("sandbox_podman_not_available")
        return False

    if not _check_image_exists():
        logger.info("sandbox_image_not_found_building")
        return _build_sandbox_image()

    return True


def get_or_create_session(conversation_id: str):
    """Get existing sandbox session or create a new persistent one."""
    from llm_sandbox import SandboxSession

    if conversation_id in _active_sessions:
        session = _active_sessions[conversation_id]
        _session_activity[conversation_id] = time.time()
        # Check if session is still alive
        try:
            # Quick health check — run a trivial command
            result = session.run("echo ok")
            if result.stdout and "ok" in result.stdout:
                return session
        except Exception:
            logger.warning("sandbox_session_stale", conversation_id=conversation_id)
            # Session is dead, remove and recreate
            try:
                session.close()
            except Exception:
                pass
            del _active_sessions[conversation_id]

    # Create volume directory for persistent storage
    volume_path = os.path.join(SANDBOX_BASE, conversation_id)
    os.makedirs(volume_path, exist_ok=True)

    logger.info(
        "sandbox_creating_session",
        conversation_id=conversation_id,
        volume_path=volume_path,
    )

    # Use runtime_configs for Podman-native volume binding
    # Podman Python API expects volumes as dict: {host_path: {"bind": container_path, "mode": mode}}
    # workdir="/sandbox" (default, ephemeral) so llm-sandbox temp .py files
    # don't pollute the persistent /workspace volume.
    # Port 9222 mapped for Chromium remote debugging (auto_browser via CDP).
    session = SandboxSession(
        lang="python",
        backend="podman",
        image=SANDBOX_IMAGE,
        keep_template=True,
        verbose=False,
        runtime_configs={
            "volumes": {
                volume_path: {"bind": "/workspace", "mode": "rw"},
            },
            "ports": {"9222/tcp": 9222},
        },
        workdir="/sandbox",
    )
    session.open()

    # Patch Podman exec to use CLI (fixes empty stdout bug in Podman Python lib)
    _patch_podman_exec(session)

    _active_sessions[conversation_id] = session

    logger.info("sandbox_session_created", conversation_id=conversation_id)
    _session_activity[conversation_id] = time.time()
    return session


def get_session(conversation_id: str):
    """Get session without creating. Returns None if not active."""
    return _active_sessions.get(conversation_id)


def destroy_session(conversation_id: str):
    """Destroy a sandbox session and cleanup container."""
    session = _active_sessions.pop(conversation_id, None)
    if session:
        try:
            session.close()
            logger.info("sandbox_session_destroyed", conversation_id=conversation_id)
        except Exception as exc:
            logger.error(
                "sandbox_session_destroy_error",
                conversation_id=conversation_id,
                error=str(exc),
            )


def destroy_all_sessions():
    """Destroy all active sandbox sessions. Called on app shutdown."""
    for cid in list(_active_sessions.keys()):
        destroy_session(cid)
    logger.info("sandbox_all_sessions_destroyed")


def cleanup_volume(conversation_id: str):
    """Delete the persistent volume for a conversation."""
    volume_path = os.path.join(SANDBOX_BASE, conversation_id)
    if os.path.isdir(volume_path):
        shutil.rmtree(volume_path, ignore_errors=True)
        logger.info("sandbox_volume_cleaned", conversation_id=conversation_id)


def list_sandbox_files(conversation_id: str, path: str = "/") -> list[dict]:
    """List files in a sandbox volume directory."""
    volume_path = os.path.join(SANDBOX_BASE, conversation_id)
    target = os.path.join(volume_path, path.lstrip("/"))
    target = os.path.abspath(target)

    # Security: ensure we don't escape the sandbox volume
    if not target.startswith(os.path.abspath(volume_path)):
        return []

    if not os.path.isdir(target):
        return []

    # Internal files/dirs created by llm-sandbox that should be hidden
    _HIDDEN = {".sandbox-pip-cache", ".sandbox-venv", "__pycache__"}

    entries = []
    try:
        for entry in os.scandir(target):
            # Skip llm-sandbox internal files and UUID-named .py temp files
            if entry.name in _HIDDEN:
                continue
            if entry.name.endswith(".py") and len(entry.name) == 35:
                # UUID hex (32) + ".py" (3) = 35 chars — llm-sandbox temp scripts
                continue
            stat = entry.stat()
            entries.append({
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "size": stat.st_size if not entry.is_dir() else 0,
                "modified_at": stat.st_mtime,
            })
    except PermissionError:
        pass

    # Sort: directories first, then by name
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


def read_sandbox_file(conversation_id: str, path: str) -> Optional[str]:
    """Read a text file from the sandbox volume. Returns None if not found."""
    volume_path = os.path.join(SANDBOX_BASE, conversation_id)
    target = os.path.join(volume_path, path.lstrip("/"))
    target = os.path.abspath(target)

    # Security: ensure we don't escape the sandbox volume
    if not target.startswith(os.path.abspath(volume_path)):
        return None

    if not os.path.isfile(target):
        return None

    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            return f.read(500_000)  # 500KB limit
    except Exception:
        return None


def get_sandbox_file_path(conversation_id: str, path: str) -> Optional[str]:
    """Get the absolute host path for a sandbox file (for downloads/exports)."""
    volume_path = os.path.join(SANDBOX_BASE, conversation_id)
    target = os.path.join(volume_path, path.lstrip("/"))
    target = os.path.abspath(target)

    # Security: ensure we don't escape the sandbox volume
    if not target.startswith(os.path.abspath(volume_path)):
        return None

    if not os.path.isfile(target):
        return None

    return target


def start_chromium_in_sandbox(conversation_id: str) -> bool:
    """Start headless Chromium inside the sandbox container with remote debugging.

    Returns True if Chromium was started successfully.
    """
    session = get_session(conversation_id)
    if not session:
        return False

    try:
        # Find Playwright's Chromium binary inside the container
        # PLAYWRIGHT_BROWSERS_PATH=/opt/playwright is set in the Dockerfile
        find_result = session.execute_command(
            "find /opt/playwright -name chrome -type f 2>/dev/null | head -1"
        )
        chrome_path = (find_result.stdout or "").strip()
        if not chrome_path:
            # Fallback: try standard playwright detection
            find_result = session.execute_command(
                "PLAYWRIGHT_BROWSERS_PATH=/opt/playwright python3 -c "
                "\"from playwright.sync_api import sync_playwright; "
                "p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()\" 2>/dev/null"
            )
            chrome_path = (find_result.stdout or "").strip()
        if not chrome_path:
            logger.warning("sandbox_chromium_not_found")
            return False
        logger.info("sandbox_chromium_found", path=chrome_path)

        # Launch Chromium in the background inside the container
        session.execute_command(
            f"{chrome_path} --headless --no-sandbox --disable-gpu "
            "--disable-dev-shm-usage "
            "--remote-debugging-address=0.0.0.0 "
            "--remote-debugging-port=9222 &"
        )
        # Give it a moment to start
        import time as _time
        _time.sleep(3)

        # Verify it's running
        check = session.execute_command("curl -s http://localhost:9222/json/version")
        if check.stdout and "webSocketDebuggerUrl" in check.stdout:
            logger.info("sandbox_chromium_started", conversation_id=conversation_id)
            return True

        logger.warning("sandbox_chromium_check_failed", stdout=check.stdout[:200] if check.stdout else "")
        return False
    except Exception as exc:
        logger.error("sandbox_chromium_start_error", error=str(exc))
        return False


def get_chromium_cdp_url(conversation_id: str) -> str | None:
    """Get the CDP WebSocket URL for Chromium running inside a sandbox container."""
    session = get_session(conversation_id)
    if not session:
        return None

    try:
        result = session.execute_command("curl -s http://localhost:9222/json/version")
        if result.stdout:
            import json
            data = json.loads(result.stdout)
            ws_url = data.get("webSocketDebuggerUrl", "")
            if ws_url:
                # Replace container's localhost with host's localhost
                # since port 9222 is mapped to host
                ws_url = ws_url.replace("0.0.0.0", "127.0.0.1")
                return ws_url
    except Exception as exc:
        logger.warning("sandbox_chromium_cdp_url_error", error=str(exc))
    return None


def cleanup_idle_sessions():
    """Destroy sandbox sessions that have been idle for more than IDLE_TIMEOUT_SECONDS.
    Called periodically from a background task.
    """
    now = time.time()
    idle_ids = [
        cid for cid, last_active in _session_activity.items()
        if now - last_active > IDLE_TIMEOUT_SECONDS and cid in _active_sessions
    ]
    for cid in idle_ids:
        logger.info("sandbox_idle_cleanup", conversation_id=cid,
                     idle_seconds=int(now - _session_activity.get(cid, 0)))
        destroy_session(cid)
        _session_activity.pop(cid, None)
