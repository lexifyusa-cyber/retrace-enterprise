"""
PTY Manager — persistent pseudo-terminal sessions per conversation.

Each conversation gets at most one PTY (real shell process).  Both the
agent's ``terminal()`` tool and users connected via WebSocket share the
same shell, so environment variables, PATH changes, and CWD persist
naturally across commands.

Output extraction (for agent):
  Each command is wrapped between a unique START and END marker via printf.
  _extract_output() pulls ONLY the text between these markers, giving the
  agent clean output.  The terminal panel (xterm.js) shows everything
  including the infrastructure — this is acceptable for a developer tool.
"""

from __future__ import annotations

import asyncio
import enum
import fcntl
import os
import pty
import re as _re
import select as _select
import signal
import struct
import termios
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from uuid import uuid4

import structlog

logger = structlog.get_logger()

SCROLLBACK_LIMIT = 200_000
IDLE_TIMEOUT_SECONDS = 30 * 60
READ_CHUNK = 4096
DEFAULT_COLS = 120
DEFAULT_ROWS = 30

PROMPT_MARKER = "__RTPS__"
_ANSI_RE = _re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\].*?\x07|\x1b\(B|\x08.")


class CommandState(enum.Enum):
    IDLE = "idle"
    RUNNING = "running"


@dataclass
class PTYSession:
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
    _shell_ready: bool = field(default=False, repr=False)
    _prompt_setup_done: bool = field(default=False, repr=False)
    _command_state: CommandState = field(default=CommandState.IDLE, repr=False)

    def touch(self):
        self.last_active_at = time.time()


class PTYManager:
    def __init__(self):
        self._sessions: dict[str, PTYSession] = {}
        self._sessions_lock = threading.Lock()
        self._reaper_thread: Optional[threading.Thread] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self._event_loop = loop

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create(self, conversation_id: str) -> PTYSession:
        with self._sessions_lock:
            if conversation_id in self._sessions:
                session = self._sessions[conversation_id]
                if not session._closed:
                    session.touch()
                    return session
                self._cleanup_session(session)

            session = self._spawn(conversation_id)
            self._sessions[conversation_id] = session

            t = threading.Thread(
                target=self._reader_loop,
                args=(session,),
                daemon=True,
                name=f"pty-reader-{conversation_id[:8]}",
            )
            session._reader_thread = t
            t.start()

            if self._reaper_thread is None or not self._reaper_thread.is_alive():
                self._reaper_thread = threading.Thread(
                    target=self._idle_reaper, daemon=True, name="pty-reaper"
                )
                self._reaper_thread.start()

            logger.info("pty_created", conversation_id=conversation_id, pid=session.pid)
            return session

    def get(self, conversation_id: str) -> Optional[PTYSession]:
        session = self._sessions.get(conversation_id)
        if session and not session._closed:
            return session
        return None

    def write(self, conversation_id: str, data: bytes):
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
        with self._sessions_lock:
            session = self._sessions.pop(conversation_id, None)
        if session:
            self._cleanup_session(session)
            logger.info("pty_destroyed", conversation_id=conversation_id)

    def destroy_all(self):
        for cid in list(self._sessions.keys()):
            self.destroy(cid)

    # ------------------------------------------------------------------
    # Shell readiness and prompt setup
    # ------------------------------------------------------------------

    def _wait_for_shell_ready(self, session: PTYSession, max_wait: float = 12.0):
        if session._shell_ready:
            return
        prompt_chars = b"$#%>"
        deadline = time.time() + max_wait
        while time.time() < deadline:
            with session._lock:
                sb = bytes(session.scrollback)
            if sb:
                stripped = sb[-512:].rstrip()
                if stripped and stripped[-1:] in prompt_chars:
                    session._shell_ready = True
                    return
            time.sleep(0.1)
        session._shell_ready = True

    def _setup_prompt(self, session: PTYSession):
        """Install a precmd/PROMPT_COMMAND hook that outputs a marker after
        each command completes.  The user's original prompt is NOT changed.
        """
        if session._prompt_setup_done:
            return
        self._wait_for_shell_ready(session)

        shell_basename = os.path.basename(session.shell)
        if shell_basename == "zsh":
            setup_cmd = (
                f"precmd() {{ local e=$?; "
                f"printf '\\n{PROMPT_MARKER}:%d:\\n' \"$e\"; "
                f"return $e; }}\n"
            )
        else:
            setup_cmd = (
                f"__rtps() {{ local e=$?; "
                f"printf '\\n{PROMPT_MARKER}:%d:\\n' \"$e\"; "
                f"return $e; }}; "
                f"PROMPT_COMMAND=__rtps\n"
            )
        try:
            os.write(session.fd, setup_cmd.encode())
        except OSError:
            pass

        # Wait briefly for the marker to confirm precmd is installed
        deadline = time.time() + 3.0
        while time.time() < deadline:
            with session._lock:
                sb = bytes(session.scrollback)
            if PROMPT_MARKER.encode() in sb:
                break
            time.sleep(0.1)

        session._prompt_setup_done = True

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def execute_command(
        self,
        conversation_id: str,
        command: str,
        timeout: float = 30.0,
        output_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Write a command to the PTY and collect output between START/END markers.

        The agent chat receives clean output (markers stripped).
        The terminal panel (xterm.js) shows everything raw — acceptable
        for a developer tool.

        Fully synchronous — safe to call from any thread.
        """
        session = self.get_or_create(conversation_id)
        self._setup_prompt(session)

        session._command_state = CommandState.RUNNING

        uid = uuid4().hex[:8]
        start_marker = f"__RTSTART_{uid}__"
        end_marker = f"__RTEND_{uid}__"

        with session._lock:
            start_pos = len(session.scrollback)

        full_cmd = (
            f"printf '\\n{start_marker}\\n' ; "
            f"{command} ; "
            f"printf '\\n{end_marker}:%d:\\n' $?\n"
        )
        try:
            os.write(session.fd, full_cmd.encode())
        except OSError as exc:
            session._command_state = CommandState.IDLE
            return f"Error writing to terminal: {exc}"

        session.touch()
        t_start = time.time()
        deadline = t_start + timeout

        end_marker_re = _re.compile(_re.escape(end_marker) + r":(\d+):")

        while time.time() < deadline:
            with session._lock:
                raw_bytes = bytes(session.scrollback[start_pos:])

            raw = raw_bytes.decode("utf-8", errors="replace")
            m_end = end_marker_re.search(raw)
            if m_end:
                exit_code = int(m_end.group(1))
                return self._extract_output(raw, start_marker, end_marker, exit_code, session, t_start)

            time.sleep(0.05)

        session._command_state = CommandState.IDLE
        logger.warning("pty_exec_timeout", conversation_id=conversation_id,
                        command=command[:80], timeout=timeout)
        with session._lock:
            partial_bytes = bytes(session.scrollback[start_pos:])
        partial = partial_bytes.decode("utf-8", errors="replace")
        result = self._extract_output(partial, start_marker, end_marker, -1, session, t_start)
        return result + f"\n[Command timed out after {int(timeout)}s — may still be running in background]"

    def _extract_output(
        self, raw: str, start_marker: str, end_marker: str,
        exit_code: int, session: PTYSession, t_start: float,
    ) -> str:
        """Extract clean command output from between START and END markers."""
        session._command_state = CommandState.IDLE
        elapsed_ms = int((time.time() - t_start) * 1000)

        clean = _ANSI_RE.sub("", raw)

        start_idx = clean.find(start_marker)
        if start_idx != -1:
            clean = clean[start_idx + len(start_marker):]

        end_marker_re = _re.compile(_re.escape(end_marker) + r":\d+:")
        m_end = end_marker_re.search(clean)
        if m_end:
            clean = clean[:m_end.start()]

        clean = clean.replace("\r\n", "\n").replace("\r", "\n")

        lines = clean.split("\n")
        filtered: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if filtered:
                    filtered.append("")
                continue
            if PROMPT_MARKER in stripped:
                continue
            if stripped == "%":
                continue
            filtered.append(line)

        output = "\n".join(filtered).strip()

        if len(output) > 100_000:
            output = output[:100_000] + "\n[output truncated at 100000 chars]"

        if exit_code > 0:
            output += f"\n[exit code: {exit_code}]"

        logger.debug("pty_exec_done", conversation_id=session.conversation_id,
                      elapsed_ms=elapsed_ms, exit_code=exit_code, output_len=len(output))
        return output or "(no output)"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _spawn(self, conversation_id: str) -> PTYSession:
        shell = os.environ.get("SHELL", "/bin/bash")
        child_pid, master_fd = pty.fork()

        if child_pid == 0:
            winsize = struct.pack("HHHH", DEFAULT_ROWS, DEFAULT_COLS, 0, 0)
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            os.execvpe(shell, [shell, "-l"], env)
            os._exit(1)
        else:
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            return PTYSession(
                conversation_id=conversation_id, pid=child_pid,
                fd=master_fd, shell=shell,
            )

    def _reader_loop(self, session: PTYSession):
        fd = session.fd
        while not session._closed:
            try:
                readable, _, _ = _select.select([fd], [], [], 0.05)
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

            # Broadcast to WebSocket clients
            if session.websocket_clients:
                text = data.decode("utf-8", errors="replace")
                loop = self._event_loop
                if loop is not None and loop.is_running():
                    dead = set()
                    for ws in list(session.websocket_clients):
                        try:
                            asyncio.run_coroutine_threadsafe(
                                ws.send_json({"type": "output", "data": text}),
                                loop,
                            )
                        except Exception:
                            dead.add(ws)
                    if dead:
                        session.websocket_clients -= dead

        session._closed = True

    def _cleanup_session(self, session: PTYSession):
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

        loop = self._event_loop
        if loop is not None and loop.is_running():
            for ws in list(session.websocket_clients):
                try:
                    asyncio.run_coroutine_threadsafe(
                        ws.send_json({"type": "exited", "code": -1}), loop,
                    )
                except Exception:
                    pass
        session.websocket_clients.clear()

    def _idle_reaper(self):
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


pty_manager = PTYManager()
