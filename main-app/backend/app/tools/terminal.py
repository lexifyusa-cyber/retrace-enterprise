"""
Terminal tool — execute shell commands via a persistent PTY session.

Each conversation gets a real shell process (bash/zsh) managed by the
PTY manager.  Environment variables, PATH, aliases, and CWD persist
naturally across calls — no more "command not found" after installing
tools in earlier commands.

For backward compatibility when no conversation_id is available, falls
back to the legacy subprocess-per-call approach.
"""

import os
import re
import subprocess
from typing import Optional

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

_BLOCKED_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "format c:",
    "dd if=/dev/zero",
    ":(){ :|:& };:",  # fork bomb
]

MAX_OUTPUT_SIZE = 100_000  # 100 KB
TIMEOUT_SECONDS = 30


def _is_blocked(command: str) -> bool:
    cmd_lower = command.lower().strip()
    return any(blocked in cmd_lower for blocked in _BLOCKED_COMMANDS)


# ---------------------------------------------------------------------------
# Factory — creates a per-conversation terminal tool
# ---------------------------------------------------------------------------

def make_terminal_for_conversation(conversation_id: str):
    """Return a ``terminal(command)`` function bound to a conversation's PTY.

    The returned function is injected into the agent's tool context so
    every ``terminal("...")`` call in the sandbox routes to the same
    persistent shell.
    """
    from app.services.pty_manager import pty_manager

    def terminal(command: str) -> str:
        """Execute a shell command in the conversation's persistent terminal.

        Environment variables, PATH, and CWD persist across calls.
        Blocked commands are rejected.  Output is truncated to 100 KB.
        Timeout: 30 seconds.
        """
        command = command.strip()
        if not command:
            return "Error: empty command"

        if _is_blocked(command):
            return "Error: command blocked for security reasons"

        try:
            return pty_manager.execute_command(
                conversation_id, command, timeout=TIMEOUT_SECONDS
            )
        except Exception as exc:
            logger.error(
                "terminal_tool_error",
                conversation_id=conversation_id,
                command=command[:80],
                error=str(exc),
            )
            return f"Error: {exc}"

    return terminal


# ---------------------------------------------------------------------------
# Legacy fallback (no conversation_id)
# ---------------------------------------------------------------------------

_cwd: str = os.getcwd()


def _extract_cd_target(command: str) -> Optional[str]:
    """Extract target directory from a cd command."""
    cmd = command.strip()
    if not cmd.startswith("cd "):
        m = re.search(r"\bcd\s+([^\s;&|]+)", cmd)
        if m:
            target = m.group(1).strip()
            for sep in ["&&", ";", "||", "|"]:
                if sep in target:
                    target = target.split(sep)[0].strip()
            return target
        return None

    parts = cmd.split(None, 2)
    if len(parts) < 2:
        return None
    target = parts[1].strip()
    for sep in ["&&", ";", "||", "|"]:
        if sep in target:
            target = target.split(sep)[0].strip()
    return target


def terminal(command: str) -> str:
    """Legacy terminal — subprocess per call, used when no PTY is available."""
    global _cwd

    command = command.strip()
    if not command:
        return "Error: empty command"

    if _is_blocked(command):
        return "Error: command blocked for security reasons"

    try:
        return _run_sync(command)
    except Exception as exc:
        logger.error("terminal_tool_error", command=command[:80], error=str(exc))
        return f"Error: {exc}"


def _run_sync(command: str) -> str:
    """Synchronous subprocess execution with CWD tracking (legacy path)."""
    global _cwd

    if not os.path.isdir(_cwd):
        _cwd = os.getcwd()

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=_cwd,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            exit_code = 124
            stderr = (stderr or "") + "\n[Command timed out after 30 seconds]"
    except Exception as exc:
        return f"Error executing command: {exc}"

    cd_target = _extract_cd_target(command)
    if cd_target:
        if cd_target == "-":
            pass
        else:
            if cd_target.startswith("~"):
                cd_target = os.path.expanduser(cd_target)
            if not os.path.isabs(cd_target):
                cd_target = os.path.join(_cwd, cd_target)
            resolved = os.path.abspath(os.path.normpath(cd_target))
            if os.path.isdir(resolved):
                _cwd = resolved

    if len(stdout) > MAX_OUTPUT_SIZE:
        stdout = stdout[:MAX_OUTPUT_SIZE] + f"\n[Output truncated at {MAX_OUTPUT_SIZE} chars]"
    if len(stderr) > MAX_OUTPUT_SIZE:
        stderr = stderr[:MAX_OUTPUT_SIZE] + f"\n[Stderr truncated at {MAX_OUTPUT_SIZE} chars]"

    combined = stdout
    if stderr:
        combined += f"\n{stderr}" if combined else stderr
    if exit_code != 0:
        combined += f"\n[exit code: {exit_code}]"

    return combined or "(no output)"
