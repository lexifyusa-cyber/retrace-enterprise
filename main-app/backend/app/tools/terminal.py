"""
Terminal tool — execute shell commands via a persistent PTY session.

Each conversation gets a real shell process (bash/zsh) managed by the
PTY manager.  Environment variables, PATH, aliases, and CWD persist
naturally across calls.

The factory ``make_terminal_for_conversation`` accepts an optional
*output_callback* that receives decoded text chunks in real time (used
by the agent service to stream terminal output via SSE).

For backward compatibility when no conversation_id is available, falls
back to a legacy subprocess-per-call approach.
"""

import os
import re
import subprocess
from typing import Callable, Optional

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
    ":(){ :|:& };:",
]

MAX_OUTPUT_SIZE = 100_000  # 100 KB
TIMEOUT_SECONDS = 30
SSH_TIMEOUT_SECONDS = 120

_SSH_COMMANDS = {"ssh", "scp", "sftp"}
_SSH_NON_INTERACTIVE_OPTS = (
    "-o BatchMode=yes "
    "-o StrictHostKeyChecking=accept-new "
    "-o ConnectTimeout=20"
)


def _is_blocked(command: str) -> bool:
    cmd_lower = command.lower().strip()
    return any(blocked in cmd_lower for blocked in _BLOCKED_COMMANDS)


def _is_ssh_command(command: str) -> bool:
    parts = command.strip().split(None, 1)
    if not parts:
        return False
    return os.path.basename(parts[0]) in _SSH_COMMANDS


def _inject_ssh_flags(command: str) -> str:
    parts = command.strip().split(None, 1)
    if not parts:
        return command

    base_cmd = os.path.basename(parts[0])
    if base_cmd not in _SSH_COMMANDS:
        return command

    if "batchmode" in command.lower():
        return command

    rest = parts[1] if len(parts) > 1 else ""
    return f"{parts[0]} {_SSH_NON_INTERACTIVE_OPTS} {rest}".strip()


# ---------------------------------------------------------------------------
# Factory — creates a per-conversation terminal tool
# ---------------------------------------------------------------------------

def make_terminal_for_conversation(
    conversation_id: str,
    output_callback: Optional[Callable[[str], None]] = None,
):
    """Return a ``terminal(command)`` function bound to a conversation's PTY.

    *output_callback*, when provided, is called with decoded text chunks as
    command output arrives in real time.  The agent service uses this to push
    ``terminal_output`` SSE events to the frontend.
    """
    from app.services.pty_manager import pty_manager

    def terminal(command: str) -> str:
        """Execute a shell command in the conversation's persistent terminal.

        Environment variables, PATH, and CWD persist across calls.
        Blocked commands are rejected.  Output is truncated to 100 KB.
        Timeout: 30 s (120 s for SSH commands).
        """
        command = command.strip()
        if not command:
            return "Error: empty command"

        if _is_blocked(command):
            return "Error: command blocked for security reasons"

        timeout = SSH_TIMEOUT_SECONDS if _is_ssh_command(command) else TIMEOUT_SECONDS
        command = _inject_ssh_flags(command)

        try:
            return pty_manager.execute_command(
                conversation_id,
                command,
                timeout=timeout,
                output_callback=output_callback,
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

    timeout = SSH_TIMEOUT_SECONDS if _is_ssh_command(command) else TIMEOUT_SECONDS
    command = _inject_ssh_flags(command)

    try:
        return _run_sync(command, timeout=timeout)
    except Exception as exc:
        logger.error("terminal_tool_error", command=command[:80], error=str(exc))
        return f"Error: {exc}"


def _run_sync(command: str, timeout: float = TIMEOUT_SECONDS) -> str:
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
            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            exit_code = 124
            stderr = (stderr or "") + f"\n[Command timed out after {int(timeout)} seconds]"
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
