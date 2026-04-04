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

# Active sessions keyed by user_id (one container per user)
_active_sessions: dict = {}
# Last activity timestamp per user_id
_session_activity: dict[str, float] = {}
# Mapping conversation_id → user_id for reverse lookups
_conversation_user_map: dict[str, str] = {}

IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes


def register_conversation(conversation_id: str, user_id: str):
    """Register which user owns a conversation (for reverse lookups)."""
    _conversation_user_map[conversation_id] = user_id


def get_user_for_conversation(conversation_id: str) -> Optional[str]:
    """Get the user_id that owns a conversation."""
    return _conversation_user_map.get(conversation_id)


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

        _podman = _podman_bin()
        cmd = [_podman, "exec"]
        if workdir:
            cmd.extend(["-w", workdir])
        cmd.append(cid)
        cmd.extend(["sh", "-c", command])

        try:
            proc = _sp.run(cmd, capture_output=True, text=True, timeout=120, env=_podman_env())
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


def _podman_env() -> dict:
    """Return environment with Homebrew paths and libkrun provider for Podman on macOS."""
    import platform
    env = os.environ.copy()
    for p in ["/opt/homebrew/bin", "/usr/local/bin"]:
        if p not in env.get("PATH", ""):
            env["PATH"] = p + ":" + env.get("PATH", "")
    # Use libkrun on Apple Silicon
    if platform.machine() == "arm64":
        env["CONTAINERS_MACHINE_PROVIDER"] = "libkrun"
    # Set correct SSH key for Podman machine (prevents auth failures)
    ssh_key = os.path.expanduser("~/.local/share/containers/podman/machine/machine")
    if os.path.isfile(ssh_key):
        env["CONTAINER_SSHKEY"] = ssh_key
    return env


def _podman_bin() -> str:
    """Get the full path to the podman binary."""
    if os.path.isfile("/opt/homebrew/bin/podman"):
        return "/opt/homebrew/bin/podman"
    return "podman"


def _check_podman_available() -> bool:
    """Check if podman is installed AND the machine is responsive.

    If the machine SSH is stale, auto-restarts it once.
    """
    import subprocess
    env = _podman_env()
    pb = _podman_bin()

    # Check binary exists
    try:
        result = subprocess.run(
            [pb, "--version"],
            capture_output=True, text=True, timeout=5, env=env,
        )
        if result.returncode != 0:
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

    # Check machine is responsive (with auto-restart on failure)
    for attempt in range(2):
        try:
            result = subprocess.run(
                [pb, "info", "--format", "{{.Host.RemoteSocket.Exists}}"],
                capture_output=True, text=True, timeout=10, env=env,
            )
            if result.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            pass

        if attempt == 0:
            # Stale SSH connection — restart the machine
            logger.warning("podman_machine_stale_restarting")
            try:
                subprocess.run([pb, "machine", "stop"], capture_output=True, timeout=30, env=env)
                subprocess.run([pb, "machine", "start"], capture_output=True, timeout=120, env=env)
                logger.info("podman_machine_restarted")
            except Exception as exc:
                logger.error("podman_machine_restart_failed", error=str(exc))
                return False

    logger.warning("podman_machine_not_responsive_after_restart")
    return False


def _check_image_exists() -> bool:
    """Check if the retrace-sandbox image is built."""
    import subprocess
    try:
        result = subprocess.run(
            [_podman_bin(), "image", "exists", SANDBOX_IMAGE],
            capture_output=True, timeout=5, env=_podman_env(),
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
            [_podman_bin(), "build", "-t", SANDBOX_IMAGE, dockerfile_dir],
            capture_output=True,
            text=True,
            timeout=600,  # 10 minutes for build
            env=_podman_env(),
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


def get_or_create_session(user_id: str):
    """Get existing sandbox session or create a new one for this user.

    One container per user — all their conversations share it.
    """
    from llm_sandbox import SandboxSession

    if user_id in _active_sessions:
        session = _active_sessions[user_id]
        _session_activity[user_id] = time.time()
        # Check if session is still alive
        try:
            result = session.run("echo ok")
            if result.stdout and "ok" in result.stdout:
                return session
        except Exception:
            logger.warning("sandbox_session_stale", user_id=user_id)
            try:
                session.close()
            except Exception:
                pass
            del _active_sessions[user_id]

    # Create volume directory for persistent storage (per user)
    volume_path = os.path.join(SANDBOX_BASE, user_id)
    os.makedirs(volume_path, exist_ok=True)

    logger.info(
        "sandbox_creating_session",
        user_id=user_id,
        volume_path=volume_path,
    )

    session = SandboxSession(
        lang="python",
        backend="podman",
        image=SANDBOX_IMAGE,
        keep_template=True,
        verbose=False,
        skip_environment_setup=True,  # Use container's system Python (has all Dockerfile packages)
        runtime_configs={
            "volumes": {
                volume_path: {"bind": "/workspace", "mode": "rw"},
            },
        },
        workdir="/sandbox",
    )
    session.open()

    # Patch Podman exec to use CLI (fixes empty stdout bug in Podman Python lib)
    _patch_podman_exec(session)

    _active_sessions[user_id] = session

    logger.info("sandbox_session_created", user_id=user_id)
    _session_activity[user_id] = time.time()
    return session


def get_session(user_id: str):
    """Get session without creating. Returns None if not active."""
    return _active_sessions.get(user_id)


def get_session_for_conversation(conversation_id: str):
    """Get session by conversation_id (resolves to user_id first)."""
    user_id = get_user_for_conversation(conversation_id)
    if user_id:
        return _active_sessions.get(user_id)
    return None


def get_any_active_session():
    """Get any active sandbox session.

    Used as fallback when conversation→user mapping isn't available yet
    (e.g. Terminal tab connects before agent runs). Safe for single-user
    desktop app. For multi-user, proper WebSocket auth is needed.
    """
    for session in _active_sessions.values():
        return session
    return None


def destroy_session(user_id: str):
    """Destroy a sandbox session and cleanup container."""
    session = _active_sessions.pop(user_id, None)
    _session_activity.pop(user_id, None)
    if session:
        # Use podman CLI to force-kill the container instead of
        # session.close() which hangs on Podman Python library socket issues.
        try:
            container = getattr(session, 'container', None)
            if container:
                cid = container.id[:12] if hasattr(container, 'id') else str(container)
                import subprocess
                subprocess.run(
                    [_podman_bin(), "rm", "-f", cid],
                    capture_output=True, timeout=10, env=_podman_env(),
                )
            logger.info("sandbox_session_destroyed", user_id=user_id)
        except Exception as exc:
            logger.error(
                "sandbox_session_destroy_error",
                user_id=user_id,
                error=str(exc),
            )


def destroy_all_sessions():
    """Destroy all active sandbox sessions. Called on app shutdown.

    Uses a timeout to prevent hanging on Podman socket issues.
    """
    import concurrent.futures
    def _destroy_all():
        for cid in list(_active_sessions.keys()):
            destroy_session(cid)

    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(_destroy_all)
            future.result(timeout=15)  # Max 15 seconds for all cleanups
    except concurrent.futures.TimeoutError:
        logger.warning("sandbox_destroy_all_timeout")
        _active_sessions.clear()
    except Exception as exc:
        logger.warning("sandbox_destroy_all_error", error=str(exc))
        _active_sessions.clear()
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
