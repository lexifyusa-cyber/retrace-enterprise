"""
Browserless Manager — manages a single shared browserless/chrome container.

One container serves all conversations. Playwright connects via CDP.
The container is started on-demand and reused across all browser sessions.
"""

import os
import subprocess
import time
from typing import Optional

import structlog

logger = structlog.get_logger()

BROWSERLESS_IMAGE = "ghcr.io/browserless/chromium:latest"
BROWSERLESS_PORT = 3000
CONTAINER_NAME = "retrace-browserless"

# Singleton state
_container_id: Optional[str] = None
_cdp_url: Optional[str] = None


def _podman_env() -> dict:
    """Return environment with Homebrew paths for Podman on macOS."""
    env = os.environ.copy()
    for p in ["/opt/homebrew/bin", "/usr/local/bin"]:
        if p not in env.get("PATH", ""):
            env["PATH"] = p + ":" + env.get("PATH", "")
    return env


def _run_podman(*args, timeout=30) -> subprocess.CompletedProcess:
    """Run a podman command with correct PATH."""
    return subprocess.run(
        ["podman", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_podman_env(),
    )


def _is_container_running() -> bool:
    """Check if the browserless container is already running."""
    try:
        result = _run_podman(
            "ps", "--filter", f"name={CONTAINER_NAME}",
            "--format", "{{.Status}}", timeout=10,
        )
        return "Up" in (result.stdout or "")
    except Exception:
        return False


def _pull_image_if_needed():
    """Pull the browserless image if not present."""
    try:
        result = _run_podman("image", "exists", BROWSERLESS_IMAGE, timeout=10)
        if result.returncode == 0:
            return True
        logger.info("browserless_pulling_image", image=BROWSERLESS_IMAGE)
        result = _run_podman("pull", BROWSERLESS_IMAGE, timeout=300)
        return result.returncode == 0
    except Exception as exc:
        logger.error("browserless_pull_failed", error=str(exc))
        return False


def ensure_running() -> Optional[str]:
    """Ensure the shared browserless container is running.

    Returns the CDP URL (e.g. 'http://localhost:3000') or None on failure.
    """
    global _container_id, _cdp_url

    # Already running?
    if _container_id and _is_container_running():
        return _cdp_url

    # Check if container exists but stopped
    try:
        result = _run_podman(
            "ps", "-a", "--filter", f"name={CONTAINER_NAME}",
            "--format", "{{.ID}}", timeout=10,
        )
        existing_id = (result.stdout or "").strip()
        if existing_id:
            # Remove the stopped container
            _run_podman("rm", "-f", existing_id, timeout=10)
    except Exception:
        pass

    # Pull image if needed
    if not _pull_image_if_needed():
        logger.error("browserless_image_not_available")
        return None

    # Start fresh container
    try:
        result = _run_podman(
            "run", "-d",
            "--name", CONTAINER_NAME,
            "-p", f"{BROWSERLESS_PORT}:3000",
            "--shm-size", "2g",
            "--restart", "unless-stopped",
            BROWSERLESS_IMAGE,
            timeout=30,
        )
        if result.returncode != 0:
            logger.error("browserless_start_failed", stderr=result.stderr[:300])
            return None

        _container_id = result.stdout.strip()[:12]
        _cdp_url = f"http://localhost:{BROWSERLESS_PORT}"

        # Wait for it to be ready
        for i in range(15):
            try:
                import httpx
                r = httpx.get(f"{_cdp_url}/json/version", timeout=2)
                if r.status_code == 200:
                    logger.info(
                        "browserless_started",
                        container_id=_container_id,
                        cdp_url=_cdp_url,
                    )
                    return _cdp_url
            except Exception:
                pass
            time.sleep(1)

        logger.warning("browserless_not_ready_after_15s")
        return _cdp_url  # Return anyway, Playwright will retry

    except Exception as exc:
        logger.error("browserless_start_error", error=str(exc))
        return None


def get_cdp_url() -> Optional[str]:
    """Get the CDP URL for the shared browserless container.

    Returns None if not running.
    """
    if _container_id and _is_container_running():
        return _cdp_url
    return None


def stop():
    """Stop and remove the shared browserless container."""
    global _container_id, _cdp_url
    try:
        _run_podman("rm", "-f", CONTAINER_NAME, timeout=10)
        logger.info("browserless_stopped")
    except Exception:
        pass
    _container_id = None
    _cdp_url = None
