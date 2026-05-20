"""
Sandbox Manager — manages Docker containers per user for isolated execution.

Each user gets a persistent Docker container.
Files persist in /data/workspaces/{user_id}/ on the host,
mounted as /workspace inside the container.
"""

import os
import shutil
import subprocess
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
SANDBOX_BASE = os.environ.get("SANDBOX_BASE", "/data/workspaces" if os.path.isdir("/data") else os.path.join(os.path.expanduser("~"), ".retrace", "workspaces"))
SANDBOX_IMAGE = "retrace-sandbox:latest"


def register_conversation(conversation_id: str, user_id: str):
    """Register which user owns a conversation (for reverse lookups)."""
    _conversation_user_map[conversation_id] = user_id


def get_user_for_conversation(conversation_id: str) -> Optional[str]:
    """Get the user_id that owns a conversation."""
    return _conversation_user_map.get(conversation_id)


def _docker_bin() -> str:
    """Get the docker binary path (supports podman via symlink)."""
    import shutil
    # Check PATH first
    path = shutil.which("docker")
    if path:
        return path
    # Check common locations for docker or podman
    for candidate in [
        os.path.expanduser("~/bin/docker"),
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
        "/opt/homebrew/bin/podman",
        "/usr/bin/docker",
    ]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "docker"


def _check_docker_available() -> bool:
    """Check if Docker daemon is running."""
    try:
        result = subprocess.run(
            [_docker_bin(), "info"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _check_image_exists() -> bool:
    """Check if the sandbox image exists."""
    try:
        result = subprocess.run(
            [_docker_bin(), "image", "inspect", SANDBOX_IMAGE],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _build_sandbox_image() -> bool:
    """Build the sandbox container image."""
    # Look for sandbox Dockerfile in multiple locations
    for candidate in [
        "/app/sandbox",  # Fly.io production
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "sandbox"),  # local dev
    ]:
        dockerfile_dir = os.path.abspath(candidate)
        if os.path.isfile(os.path.join(dockerfile_dir, "Dockerfile")):
            break
    else:
        logger.error("sandbox_dockerfile_missing")
        return False

    logger.info("sandbox_building_image", image=SANDBOX_IMAGE, context=dockerfile_dir)
    try:
        result = subprocess.run(
            [_docker_bin(), "build", "-t", SANDBOX_IMAGE, dockerfile_dir],
            capture_output=True, text=True, timeout=600,
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
    if not _check_docker_available():
        return {
            "available": False,
            "reason": "Docker is not running.",
        }

    if not _check_image_exists():
        return {
            "available": False,
            "reason": "Sandbox image not built. Building automatically...",
            "needs_build": True,
        }

    return {"available": True}


def _image_needs_rebuild() -> bool:
    """Check if the cached sandbox image is outdated and needs rebuilding.

    Detects the old browser_agent.py-based image that lacks entrypoint.sh.
    """
    if not _check_image_exists():
        return True
    try:
        result = subprocess.run(
            [_docker_bin(), "run", "--rm", SANDBOX_IMAGE, "test", "-f", "/opt/entrypoint.sh"],
            capture_output=True, timeout=15,
        )
        if result.returncode != 0:
            logger.info("sandbox_image_outdated", reason="missing /opt/entrypoint.sh")
            return True
    except Exception:
        pass
    return False


def ensure_sandbox_ready() -> bool:
    """Ensure Docker is available and image is built. Returns True if ready."""
    if not _check_docker_available():
        logger.warning("sandbox_docker_not_available")
        return False

    if _image_needs_rebuild():
        logger.info("sandbox_image_not_found_or_outdated_building")
        return _build_sandbox_image()

    return True


class DockerSandboxSession:
    """Lightweight wrapper matching the interface expected by agent_service and tools."""

    def __init__(self, container_id: str, volume_path: str):
        self.container_id = container_id
        self.volume_path = volume_path

        # Mimic llm-sandbox container object for PTY manager compatibility
        class _Container:
            def __init__(self, cid):
                self.id = cid
        self.container = _Container(container_id)

    def execute_command(self, command: str, workdir: str = None, stdin_data: str | bytes | None = None):
        """Execute a command inside the Docker container.

        If stdin_data is provided, the command is run with `docker exec -i` and
        the data is piped to the process's stdin. This avoids ARG_MAX limits
        when passing large payloads (e.g. write_file content).
        """
        cmd = [_docker_bin(), "exec"]
        if stdin_data is not None:
            cmd.append("-i")
        if workdir:
            cmd.extend(["-w", workdir])
        cmd.extend([self.container_id[:12], "sh", "-c", command])

        # Browser commands need longer timeout (heavy pages can take 90s+)
        timeout = 120 if ("browser_control" in command or "browser_daemon" in command) else 60

        try:
            if stdin_data is not None:
                input_bytes = stdin_data.encode("utf-8") if isinstance(stdin_data, str) else stdin_data
                proc = subprocess.run(
                    cmd, capture_output=True, input=input_bytes, timeout=timeout,
                )
                stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
                stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            else:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                )
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""

            class Result:
                pass
            result = Result()
            result.stdout = stdout
            result.stderr = stderr
            result.returncode = proc.returncode
            # llm-sandbox compatibility: some code reads result as tuple
            return result

        except subprocess.TimeoutExpired:
            class Result:
                pass
            result = Result()
            result.stdout = ""
            result.stderr = f"Command timed out after {timeout}s"
            result.returncode = 124
            return result

    def close(self):
        """Stop and remove the container."""
        try:
            subprocess.run(
                [_docker_bin(), "rm", "-f", self.container_id[:12]],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass


def get_or_create_session(user_id: str):
    """Get existing sandbox session or create a new one for this user."""

    if user_id in _active_sessions:
        session = _active_sessions[user_id]
        _session_activity[user_id] = time.time()
        # Health check
        try:
            result = session.execute_command("echo ok")
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

    # Clean stale Chromium lock files from previous containers.
    # These are symlinks that aren't visible inside the new container
    # (macOS/podman volume mount issue), so we must remove them from the host.
    chrome_profile = os.path.join(volume_path, ".chrome-profile")
    if os.path.isdir(chrome_profile):
        for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lock_path = os.path.join(chrome_profile, lock)
            try:
                if os.path.exists(lock_path) or os.path.islink(lock_path):
                    os.unlink(lock_path)
                    logger.info("sandbox_removed_lock_file", path=lock_path)
            except Exception as e:
                logger.warning("sandbox_lock_cleanup_failed", path=lock_path, error=str(e))

    container_name = f"retrace-{user_id[:12]}"

    # Remove any existing container with same name
    subprocess.run(
        [_docker_bin(), "rm", "-f", container_name],
        capture_output=True, timeout=10,
    )

    logger.info("sandbox_creating_session", user_id=user_id, volume_path=volume_path)

    try:
        result = subprocess.run(
            [
                _docker_bin(), "run", "-d",
                "--name", container_name,
                "-v", f"{volume_path}:/workspace",
                "-w", "/workspace",
                "--network", "bridge",  # Bridge network for internet access
                "--shm-size=256m",  # Chromium needs more than default 64MB shm
                SANDBOX_IMAGE,
                # Explicitly run entrypoint.sh if present (new image),
                # otherwise fall back to tail (old cached image).
                "sh", "-c",
                "test -x /opt/entrypoint.sh && exec /opt/entrypoint.sh || exec tail -f /dev/null",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.error("sandbox_container_create_failed", stderr=result.stderr[:300])
            return None

        container_id = result.stdout.strip()
        session = DockerSandboxSession(container_id, volume_path)

        _active_sessions[user_id] = session
        _session_activity[user_id] = time.time()
        logger.info("sandbox_session_created", user_id=user_id, container=container_id[:12])
        return session

    except Exception as exc:
        logger.error("sandbox_session_create_error", user_id=user_id, error=str(exc))
        return None


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
    """Get any active sandbox session (fallback)."""
    for session in _active_sessions.values():
        return session
    return None


def destroy_session(user_id: str):
    """Destroy a sandbox session and cleanup container."""
    session = _active_sessions.pop(user_id, None)
    _session_activity.pop(user_id, None)
    if session:
        try:
            session.close()
            logger.info("sandbox_session_destroyed", user_id=user_id)
        except Exception as exc:
            logger.error("sandbox_session_destroy_error", user_id=user_id, error=str(exc))


def destroy_all_sessions():
    """Destroy all active sandbox sessions. Called on app shutdown."""
    import concurrent.futures
    def _destroy_all():
        for uid in list(_active_sessions.keys()):
            destroy_session(uid)

    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(_destroy_all)
            future.result(timeout=15)
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
    # Resolve user_id from conversation_id for workspace path
    user_id = get_user_for_conversation(conversation_id)
    base = os.path.join(SANDBOX_BASE, user_id) if user_id else os.path.join(SANDBOX_BASE, conversation_id)
    target = os.path.join(base, path.lstrip("/"))
    target = os.path.abspath(target)

    if not target.startswith(os.path.abspath(base)):
        return []

    if not os.path.isdir(target):
        return []

    _HIDDEN = {".sandbox-pip-cache", ".sandbox-venv", "__pycache__"}

    entries = []
    try:
        for entry in os.scandir(target):
            if entry.name in _HIDDEN:
                continue
            if entry.name.endswith(".py") and len(entry.name) == 35:
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

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


def read_sandbox_file(conversation_id: str, path: str) -> Optional[str]:
    """Read a text file from the sandbox volume."""
    user_id = get_user_for_conversation(conversation_id)
    base = os.path.join(SANDBOX_BASE, user_id) if user_id else os.path.join(SANDBOX_BASE, conversation_id)
    target = os.path.join(base, path.lstrip("/"))
    target = os.path.abspath(target)

    if not target.startswith(os.path.abspath(base)):
        return None

    if not os.path.isfile(target):
        return None

    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            return f.read(500_000)
    except Exception:
        return None


def get_sandbox_file_path(conversation_id: str, path: str) -> Optional[str]:
    """Get the absolute host path for a sandbox file."""
    user_id = get_user_for_conversation(conversation_id)
    base = os.path.join(SANDBOX_BASE, user_id) if user_id else os.path.join(SANDBOX_BASE, conversation_id)
    target = os.path.join(base, path.lstrip("/"))
    target = os.path.abspath(target)

    if not target.startswith(os.path.abspath(base)):
        return None

    if not os.path.isfile(target):
        return None

    return target


def cleanup_idle_sessions():
    """Destroy sandbox sessions that have been idle for more than IDLE_TIMEOUT_SECONDS."""
    now = time.time()
    idle_ids = [
        uid for uid, last_active in _session_activity.items()
        if now - last_active > IDLE_TIMEOUT_SECONDS and uid in _active_sessions
    ]
    for uid in idle_ids:
        logger.info("sandbox_idle_cleanup", user_id=uid,
                     idle_seconds=int(now - _session_activity.get(uid, 0)))
        destroy_session(uid)
        _session_activity.pop(uid, None)


def cleanup_old_workspaces(max_age_days: int = 2):
    """Delete workspace directories older than max_age_days."""
    if not os.path.isdir(SANDBOX_BASE):
        return
    now = time.time()
    max_age_seconds = max_age_days * 86400
    for entry in os.scandir(SANDBOX_BASE):
        if entry.is_dir():
            try:
                age = now - entry.stat().st_mtime
                if age > max_age_seconds:
                    shutil.rmtree(entry.path, ignore_errors=True)
                    logger.info("sandbox_old_workspace_cleaned", path=entry.name, age_days=int(age / 86400))
            except Exception:
                pass
