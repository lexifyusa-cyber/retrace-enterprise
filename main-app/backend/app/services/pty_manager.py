"""
PTY Manager — persistent pseudo-terminal sessions per conversation.

Each conversation gets at most one PTY (real shell process).  Both the
agent's ``terminal()`` tool and users connected via WebSocket share the
same shell, so environment variables, PATH changes, and CWD persist
naturally across commands.

Lifecycle:
  - Created lazily on first use (agent ``terminal()`` call or WS connect).
  - Destroyed when the conversation is deleted or after an idle timeout.
  - Lost on server restart (ephemeral); the frontend shows "Session ended".

Threading model:
  - Each PTY has a daemon reader thread that fills a scrollback buffer.
  - ``execute_command()`` is fully synchronous (safe in LangGraph sandbox threads).
  - WebSocket broadcast is scheduled onto the asyncio loop from the reader thread.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import select as _select
import signal
import struct
import termios
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

import structlog

logger = structlog.get_logger()

SCROLLBACK_LIMIT = 200_000  # bytes kept in the ring buffer
IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 min
READ_CHUNK = 4096
DEFAULT_COLS = 120
DEFAULT_ROWS = 30


@dataclass
class PTYSession:
    """A single PTY shell session bound to a conversation."""

    conversation_id: str
    pid: int
    fd: int
    shell: str
    cols: int = DEFAULT_COLS
    rows: int = DEFAULT_ROWS
    scrollback: bytearray = field(default_factory=bytearray)
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    websocket_clients: set = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _reader_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    def touch(self):
        self.last_active_at = time.time()


class PTYManager:
    """Manages one PTY session per conversation_id."""

    def __init__(self):
        self._sessions: dict[str, PTYSession] = {}
        self._sessions_lock = threading.Lock()
        self._reaper_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create(self, conversation_id: str) -> PTYSession:
        """Return the existing session or spawn a new shell."""
        with self._sessions_lock:
            if conversation_id in self._sessions:
                session = self._sessions[conversation_id]
                if not session._closed:
                    session.touch()
                    return session
                self._cleanup_session(session)

            session = self._spawn(conversation_id)
            self._sessions[conversation_id] = session

            # Start a daemon reader thread for this PTY
            t = threading.Thread(
                target=self._reader_loop,
                args=(session,),
                daemon=True,
                name=f"pty-reader-{conversation_id[:8]}",
            )
            session._reader_thread = t
            t.start()

            # Ensure idle reaper is running
            if self._reaper_thread is None or not self._reaper_thread.is_alive():
                self._reaper_thread = threading.Thread(
                    target=self._idle_reaper, daemon=True, name="pty-reaper"
                )
                self._reaper_thread.start()

            logger.info("pty_created", conversation_id=conversation_id, pid=session.pid)
            return session

    def get(self, conversation_id: str) -> Optional[PTYSession]:
        """Return existing session or None."""
        session = self._sessions.get(conversation_id)
        if session and not session._closed:
            return session
        return None

    def write(self, conversation_id: str, data: bytes):
        """Write raw bytes to the PTY stdin."""
        session = self.get(conversation_id)
        if session is None:
            raise ValueError(f"No PTY session for conversation {conversation_id}")
        session.touch()
        try:
            os.write(session.fd, data)
        except OSError as exc:
            logger.error("pty_write_error", conversation_id=conversation_id, error=str(exc))
            session._closed = True

    def resize(self, conversation_id: str, cols: int, rows: int):
        """Send a TIOCSWINSZ to resize the PTY."""
        session = self.get(conversation_id)
        if session is None:
            return
        session.cols = cols
        session.rows = rows
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(session.fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def destroy(self, conversation_id: str):
        """Kill the shell and free resources for a conversation."""
        with self._sessions_lock:
            session = self._sessions.pop(conversation_id, None)
        if session:
            self._cleanup_session(session)
            logger.info("pty_destroyed", conversation_id=conversation_id)

    def destroy_all(self):
        """Shutdown hook — kill every session."""
        for cid in list(self._sessions.keys()):
            self.destroy(cid)

    def execute_command(self, conversation_id: str, command: str, timeout: float = 30.0) -> str:
        """Write a command to the PTY and collect output until a sentinel appears.

        Fully synchronous — safe to call from any thread (including LangGraph sandbox).
        The PTY output contains ANSI escape codes and ``\\r\\n`` line endings;
        this method strips them before returning clean text.
        """
        import re as _re

        session = self.get_or_create(conversation_id)
        sentinel = f"__RTDONE_{uuid4().hex[:8]}__"

        with session._lock:
            start_pos = len(session.scrollback)

        full_cmd = f"{command} ; echo {sentinel} $?\n"
        try:
            os.write(session.fd, full_cmd.encode())
        except OSError as exc:
            return f"Error writing to terminal: {exc}"

        session.touch()

        deadline = time.time() + timeout
        sentinel_bytes = sentinel.encode()

        while time.time() < deadline:
            with session._lock:
                new_output = bytes(session.scrollback[start_pos:])

            idx = new_output.find(sentinel_bytes)
            if idx != -1:
                raw = new_output.decode("utf-8", errors="replace")

                # Find sentinel line and extract exit code
                sentinel_pattern = _re.escape(sentinel) + r"\s+(\d+)"
                m = _re.search(sentinel_pattern, raw)
                exit_code = 0
                if m:
                    try:
                        exit_code = int(m.group(1))
                    except ValueError:
                        pass
                    # Take everything before the sentinel
                    raw = raw[: m.start()]

                # Strip ANSI escape sequences
                ansi_re = _re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\].*?\x07|\x08.")
                clean = ansi_re.sub("", raw)

                # Normalize line endings and split
                clean = clean.replace("\r\n", "\n").replace("\r", "\n")
                lines = clean.split("\n")

                # Drop the echoed command line (first non-empty line usually contains it)
                cmd_prefix = command.strip()[:30]
                filtered: list[str] = []
                skipped_echo = False
                for line in lines:
                    stripped = line.strip()
                    if not skipped_echo and cmd_prefix and cmd_prefix in stripped:
                        skipped_echo = True
                        continue
                    # Skip empty prompt lines (contain only whitespace)
                    if not stripped:
                        if filtered:
                            filtered.append("")
                        continue
                    filtered.append(line)

                output = "\n".join(filtered).strip()

                if len(output) > 100_000:
                    output = output[:100_000] + "\n[output truncated at 100000 chars]"

                if exit_code != 0:
                    output += f"\n[exit code: {exit_code}]"

                return output or "(no output)"

            time.sleep(0.05)

        return "[Command timed out after 30 seconds]"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _spawn(self, conversation_id: str) -> PTYSession:
        """Fork a new PTY shell process via ``pty.fork()``."""
        shell = os.environ.get("SHELL", "/bin/bash")

        child_pid, master_fd = pty.fork()

        if child_pid == 0:
            # --- Child process (slave PTY is already stdin/stdout/stderr) ---
            winsize = struct.pack("HHHH", DEFAULT_ROWS, DEFAULT_COLS, 0, 0)
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

            env = os.environ.copy()
            env["TERM"] = "xterm-256color"

            # -l → login shell so ~/.zshrc / ~/.bash_profile are sourced
            os.execvpe(shell, [shell, "-l"], env)
            os._exit(1)
        else:
            # --- Parent process ---
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            return PTYSession(
                conversation_id=conversation_id,
                pid=child_pid,
                fd=master_fd,
                shell=shell,
            )

    def _reader_loop(self, session: PTYSession):
        """Background thread: read PTY output → scrollback + WS broadcast."""
        fd = session.fd

        while not session._closed:
            try:
                readable, _, _ = _select.select([fd], [], [], 0.1)
            except (ValueError, OSError):
                break

            if not readable:
                continue

            try:
                data = os.read(fd, READ_CHUNK)
            except OSError:
                break

            if not data:
                break

            with session._lock:
                session.scrollback.extend(data)
                if len(session.scrollback) > SCROLLBACK_LIMIT:
                    excess = len(session.scrollback) - SCROLLBACK_LIMIT
                    del session.scrollback[:excess]

            session.touch()

            # Broadcast to WebSocket clients (schedule on asyncio loop if available)
            if session.websocket_clients:
                text = data.decode("utf-8", errors="replace")
                dead = set()
                for ws in list(session.websocket_clients):
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                ws.send_json({"type": "output", "data": text}),
                                loop,
                            )
                        # If no loop is running, skip WS broadcast (test/CLI context)
                    except Exception:
                        dead.add(ws)
                session.websocket_clients -= dead

        session._closed = True

    def _cleanup_session(self, session: PTYSession):
        """Kill process and close fd."""
        session._closed = True

        try:
            os.close(session.fd)
        except OSError:
            pass

        try:
            os.kill(session.pid, signal.SIGTERM)
            try:
                os.waitpid(session.pid, os.WNOHANG)
            except ChildProcessError:
                pass
        except ProcessLookupError:
            pass

        # Notify connected WS clients
        for ws in list(session.websocket_clients):
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        ws.send_json({"type": "exited", "code": -1}),
                        loop,
                    )
            except Exception:
                pass
        session.websocket_clients.clear()

    def _idle_reaper(self):
        """Background thread that kills PTYs idle for too long."""
        while True:
            time.sleep(60)
            now = time.time()
            stale = [
                cid for cid, s in list(self._sessions.items())
                if not s._closed and (now - s.last_active_at) > IDLE_TIMEOUT_SECONDS
            ]
            for cid in stale:
                logger.info("pty_idle_reap", conversation_id=cid)
                self.destroy(cid)
            with self._sessions_lock:
                if not self._sessions:
                    break


# Global singleton
pty_manager = PTYManager()
