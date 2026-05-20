#!/usr/bin/env python3
"""
Browser Control — thin HTTP client that forwards actions to browser_daemon.

Replaces browser_agent.py as the entry point called by the host. Where
browser_agent.py imported Playwright and ran the full browser stack inside
a fresh subprocess (~2 seconds of cold start per invocation), this script
just POSTs a JSON action to the long-lived browser_daemon running at
http://127.0.0.1:9223.

Python startup + an HTTP round-trip is ~200ms instead of ~2000ms.

If the daemon isn't running (e.g. just after container boot, or it
crashed), this script starts it via subprocess.Popen and waits for
/health to respond before forwarding the action.

Usage:
    python3 /opt/browser_control.py '{"action":"navigate","url":"https://google.com"}'
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = 9223
DAEMON_SCRIPT = "/opt/browser_daemon.py"
DAEMON_PIDFILE = "/tmp/browser_daemon.pid"
DAEMON_LOG = "/tmp/browser_daemon.log"

HEALTH_URL = f"http://{DAEMON_HOST}:{DAEMON_PORT}/health"
ACTION_URL = f"http://{DAEMON_HOST}:{DAEMON_PORT}/action"


def _http_get_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return None


def _http_post_json(url: str, payload: dict, timeout: float = 180.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _daemon_is_running() -> bool:
    """Quick probe: is the daemon HTTP server answering?"""
    return _http_get_json(HEALTH_URL, timeout=1.0) is not None


def _start_daemon() -> bool:
    """Spawn browser_daemon.py in the background and wait for it to be ready.

    Returns True if the daemon is answering /health within 30 seconds.
    """
    # Detach so the daemon outlives this control script.
    try:
        with open(DAEMON_LOG, "ab", buffering=0) as log_fp:
            proc = subprocess.Popen(
                ["python3", DAEMON_SCRIPT],
                stdout=log_fp,
                stderr=log_fp,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # detach from this process
            )
        try:
            with open(DAEMON_PIDFILE, "w") as f:
                f.write(str(proc.pid))
        except Exception:
            pass
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": f"Failed to launch daemon: {exc}"})
        )
        return False

    # Poll /health for up to 30 seconds (first start includes Chromium boot)
    deadline = time.time() + 30.0
    while time.time() < deadline:
        if _daemon_is_running():
            return True
        time.sleep(0.3)
    return False


def _ensure_daemon_ready() -> bool:
    if _daemon_is_running():
        return True
    return _start_daemon()


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "No action provided"}))
        sys.exit(1)

    try:
        action_data = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"Invalid JSON: {exc}"}))
        sys.exit(1)

    if not _ensure_daemon_ready():
        print(json.dumps({
            "ok": False,
            "error": "browser_daemon did not become ready within 30s",
        }))
        sys.exit(1)

    try:
        result = _http_post_json(ACTION_URL, action_data)
    except urllib.error.URLError as exc:
        # Daemon died between the health check and our request — try once more.
        if _start_daemon():
            try:
                result = _http_post_json(ACTION_URL, action_data)
            except Exception as exc2:
                print(json.dumps({"ok": False, "error": f"Daemon retry failed: {exc2}"}))
                sys.exit(1)
        else:
            print(json.dumps({"ok": False, "error": f"Daemon unreachable: {exc}"}))
            sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"HTTP error: {exc}"}))
        sys.exit(1)

    print(json.dumps(result))


if __name__ == "__main__":
    main()
