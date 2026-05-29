"""
Stream Monitor Service — live process-stream watches (Phase 6).

The streaming counterpart to the poll-and-diff ToolMonitorService. Where
`monitor_command` runs a command ONCE per tick and diffs the result, this
runs a LONG-RUNNING command (`tail -f`, `journalctl -f`, `kubectl logs -f`)
and reads its stdout line-by-line, firing on lines that match `fire_when`,
rate-limited by a coalescing debounce. This closes the one real-time gap vs
Claude Code's `Monitor` tool: sub-second latency, every line seen.

Lifecycle differs from ToolMonitorService: no APScheduler interval jobs.
Each monitor owns one long-lived asyncio task (PTY-manager pattern — a dict
of tasks keyed by monitor_id, cancelled cleanly on stop). State lives in the
shared `tool_monitors` table with target_kind="stream",
target_json={"command": ..., "debounce_seconds": N}; ToolMonitorService
ignores stream rows and this service loads only them.

Reuses: monitor_predicate.should_fire (filtering), followup_scheduler.
_inject_prompt (delivery), the tool_monitors table.

Gated by RETRACE_ENABLE_STREAM_MONITOR — long-lived subprocesses are higher
risk than one-shot ticks, so the feature has an explicit kill-switch.
"""

import asyncio
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog

from app.services.monitor_predicate import should_fire

logger = structlog.get_logger()


_OUTPUT_LINE_CAP = 4000        # per-line char cap (defends against giant lines)
_BUFFER_LINE_CAP = 200         # max coalesced lines held between fires
_FIRE_SNIPPET_CAP = 8000       # max chars of coalesced lines handed to the LLM
_GRACE_KILL_SECONDS = 3.0      # SIGTERM → wait → SIGKILL on teardown
_MIN_DEBOUNCE = 3
_MIN_DURATION = 15


def stream_monitor_enabled() -> bool:
    return (os.environ.get("RETRACE_ENABLE_STREAM_MONITOR", "1").strip()
            not in ("", "0", "false", "False"))


class StreamMonitorService:
    """Manages live process-stream watches as one asyncio task per monitor."""

    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        logger.info("stream_monitor_service_started", enabled=stream_monitor_enabled())
        if stream_monitor_enabled():
            await self._load_existing()

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for monitor_id, task in list(self._tasks.items()):
            task.cancel()
        # Give cancelled tasks a moment to run their finally-blocks (which kill
        # the subprocess groups). Best-effort.
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        logger.info("stream_monitor_service_stopped")

    async def _load_existing(self) -> None:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select

        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(Row).where(
                        Row.is_active == True,
                        Row.target_kind.in_(("stream", "websocket")),
                    )
                )
                rows = result.scalars().all()
                added = pruned = 0
                now = datetime.utcnow()
                for row in rows:
                    if row.expires_at <= now:
                        row.is_active = False
                        row.last_error = "expired before service restart"
                        pruned += 1
                        continue
                    self._spawn(row.monitor_id)
                    added += 1
                if pruned:
                    await session.commit()
                logger.info("stream_monitor_loaded", active=added, expired=pruned)
        except Exception as exc:
            logger.warning("stream_monitor_load_failed", error=str(exc))

    # ─── Public API ─────────────────────────────────────────────────────────
    async def add(
        self,
        user_id: str,
        conversation_id: str,
        command: str,
        duration_seconds: int,
        prompt_on_change: str,
        fire_when: str = "contains:ERROR",
        debounce_seconds: int = 5,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row

        if not stream_monitor_enabled():
            raise ValueError(
                "stream monitoring is disabled (set RETRACE_ENABLE_STREAM_MONITOR=1)"
            )
        if not command.strip():
            raise ValueError("command must be non-empty")
        if duration_seconds < _MIN_DURATION:
            raise ValueError(f"duration_seconds must be >= {_MIN_DURATION}")
        if debounce_seconds < _MIN_DEBOUNCE:
            raise ValueError(f"debounce_seconds must be >= {_MIN_DEBOUNCE}")

        async with async_session_maker() as session:
            row = Row(
                user_id=user_id,
                conversation_id=conversation_id,
                target_kind="stream",
                target_json={"command": command, "debounce_seconds": debounce_seconds},
                every_seconds=0,  # unused for streams
                expires_at=datetime.utcnow() + timedelta(seconds=duration_seconds),
                fire_when=(fire_when or "contains:ERROR").strip(),
                prompt_on_change=prompt_on_change,
                description=description,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            payload = row.to_dict()

        self._spawn(payload["monitor_id"])
        logger.info("stream_monitor_added", monitor_id=payload["monitor_id"],
                    command=command[:120], debounce_seconds=debounce_seconds,
                    fire_when=payload["fire_when"])
        return payload

    async def add_websocket(
        self,
        user_id: str,
        conversation_id: str,
        url: str,
        duration_seconds: int,
        prompt_on_change: str,
        subscribe: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        fire_when: str = "changed",
        debounce_seconds: int = 5,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row

        if not stream_monitor_enabled():
            raise ValueError(
                "stream monitoring is disabled (set RETRACE_ENABLE_STREAM_MONITOR=1)"
            )
        if not (isinstance(url, str) and url.startswith(("ws://", "wss://"))):
            raise ValueError("url must start with ws:// or wss://")
        if duration_seconds < _MIN_DURATION:
            raise ValueError(f"duration_seconds must be >= {_MIN_DURATION}")
        if debounce_seconds < _MIN_DEBOUNCE:
            raise ValueError(f"debounce_seconds must be >= {_MIN_DEBOUNCE}")

        async with async_session_maker() as session:
            row = Row(
                user_id=user_id,
                conversation_id=conversation_id,
                target_kind="websocket",
                target_json={
                    "url": url,
                    "subscribe": subscribe or None,
                    "headers": headers or None,
                    "debounce_seconds": debounce_seconds,
                },
                every_seconds=0,
                expires_at=datetime.utcnow() + timedelta(seconds=duration_seconds),
                fire_when=(fire_when or "changed").strip(),
                prompt_on_change=prompt_on_change,
                description=description,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            payload = row.to_dict()

        self._spawn(payload["monitor_id"])
        logger.info("websocket_monitor_added", monitor_id=payload["monitor_id"],
                    url=url[:120], debounce_seconds=debounce_seconds,
                    fire_when=payload["fire_when"])
        return payload

    async def cancel(self, monitor_id: str, user_id: str) -> bool:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select

        async with async_session_maker() as session:
            result = await session.execute(
                select(Row).where(Row.monitor_id == monitor_id, Row.user_id == user_id)
            )
            row = result.scalar_one_or_none()
            if not row or not row.is_active:
                return False
            row.is_active = False
            await session.commit()
        task = self._tasks.pop(monitor_id, None)
        if task:
            task.cancel()
        logger.info("stream_monitor_cancelled", monitor_id=monitor_id)
        return True

    async def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select

        async with async_session_maker() as session:
            result = await session.execute(
                select(Row)
                .where(Row.user_id == user_id, Row.is_active == True,
                       Row.target_kind.in_(("stream", "websocket")))
                .order_by(Row.created_at.desc())
            )
            return [r.to_dict() for r in result.scalars().all()]

    # ─── Task lifecycle ─────────────────────────────────────────────────────
    def _spawn(self, monitor_id: str) -> None:
        if monitor_id in self._tasks and not self._tasks[monitor_id].done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("stream_monitor_spawn_no_loop", monitor_id=monitor_id)
            return
        task = loop.create_task(self._dispatch(monitor_id))
        self._tasks[monitor_id] = task

    async def _dispatch(self, monitor_id: str) -> None:
        """Route to the source-specific run loop by the row's target_kind."""
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select
        async with async_session_maker() as session:
            kind = (await session.execute(
                select(Row.target_kind).where(Row.monitor_id == monitor_id)
            )).scalar_one_or_none()
        if kind == "websocket":
            await self._run_socket(monitor_id)
        else:
            await self._run_stream(monitor_id)

    async def _run_stream(self, monitor_id: str) -> None:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select

        async with async_session_maker() as session:
            row = (await session.execute(
                select(Row).where(Row.monitor_id == monitor_id)
            )).scalar_one_or_none()
            if not row or not row.is_active:
                return
            user_id = row.user_id
            conversation_id = row.conversation_id
            tj = dict(row.target_json or {})
            command = tj.get("command", "")
            debounce = max(_MIN_DEBOUNCE, int(tj.get("debounce_seconds", 5)))
            fire_when = row.fire_when or "contains:ERROR"
            prompt_on_change = row.prompt_on_change
            description = row.description or f"stream `{command[:60]}`"
            expires_at = row.expires_at

        deadline = expires_at.replace(tzinfo=timezone.utc).timestamp()
        proc: Optional[asyncio.subprocess.Process] = None
        buffer: List[str] = []
        last_fire = 0.0
        last_state = False
        lines_read = 0
        ended_reason = "window ended"
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # own process group → kill the whole pipeline
            )
            while True:
                now = time.monotonic()
                wall = datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()
                if wall >= deadline:
                    ended_reason = "window ended"
                    break

                # Read a line, but wake at least every `debounce` so we can
                # flush a non-empty buffer even when the stream goes quiet.
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=debounce)
                except asyncio.TimeoutError:
                    raw = None  # quiet window → fall through to flush check

                if raw == b"":  # EOF — command exited on its own
                    ended_reason = "stream ended early (command exited)"
                    break

                if raw:
                    lines_read += 1
                    line = raw.decode("utf-8", "replace").rstrip("\n")[:_OUTPUT_LINE_CAP]
                    fire, last_state = should_fire(
                        fire_when, line, changed=True, last_state=last_state
                    )
                    if fire and len(buffer) < _BUFFER_LINE_CAP:
                        buffer.append(line)

                # Coalescing debounce: flush buffered matches at most once per
                # `debounce` seconds.
                if buffer and (now - last_fire) >= debounce:
                    await self._fire(monitor_id, user_id, conversation_id,
                                     description, command, fire_when, buffer)
                    buffer.clear()
                    last_fire = now
        except asyncio.CancelledError:
            ended_reason = "cancelled"
            raise
        except Exception as exc:
            ended_reason = f"error: {exc}"
            logger.warning("stream_monitor_run_failed", monitor_id=monitor_id, error=str(exc))
        finally:
            await self._kill(proc)
            self._tasks.pop(monitor_id, None)
            # Flush any remaining buffered matches before closing out.
            if buffer and ended_reason != "cancelled":
                try:
                    await self._fire(monitor_id, user_id, conversation_id,
                                     description, command, fire_when, buffer)
                except Exception:
                    pass
            if ended_reason != "cancelled":
                await self._finalize(monitor_id, user_id, conversation_id,
                                     description, command, lines_read, ended_reason)

    async def _run_socket(self, monitor_id: str) -> None:
        """Persistent WebSocket watch: connect, optionally send a subscribe
        frame, consume messages, fire (debounced) on matching ones. Reconnects
        with exponential backoff on drop until the window expires."""
        import json as _json
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select
        import websockets

        async with async_session_maker() as session:
            row = (await session.execute(
                select(Row).where(Row.monitor_id == monitor_id)
            )).scalar_one_or_none()
            if not row or not row.is_active:
                return
            user_id = row.user_id
            conversation_id = row.conversation_id
            tj = dict(row.target_json or {})
            url = tj.get("url", "")
            subscribe = tj.get("subscribe")
            headers = tj.get("headers") or None
            debounce = max(_MIN_DEBOUNCE, int(tj.get("debounce_seconds", 5)))
            fire_when = row.fire_when or "changed"
            prompt_on_change = row.prompt_on_change
            description = row.description or f"websocket {url[:60]}"
            expires_at = row.expires_at

        deadline = expires_at.replace(tzinfo=timezone.utc).timestamp()
        buffer: List[str] = []
        last_fire = 0.0
        last_state = False
        msgs = 0
        ended_reason = "window ended"
        backoff = 1.0

        def _remaining() -> float:
            return deadline - datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()

        try:
            while _remaining() > 0:
                try:
                    async with websockets.connect(
                        url,
                        additional_headers=headers,
                        open_timeout=10,
                        close_timeout=5,
                        ping_interval=20,
                    ) as ws:
                        backoff = 1.0  # reset on a successful connect
                        if subscribe:
                            await ws.send(subscribe)
                        while True:
                            rem = _remaining()
                            if rem <= 0:
                                ended_reason = "window ended"
                                break
                            try:
                                raw = await asyncio.wait_for(
                                    ws.recv(), timeout=min(debounce, rem)
                                )
                            except asyncio.TimeoutError:
                                raw = None  # quiet window → flush check below
                            now = time.monotonic()
                            if raw is not None:
                                msgs += 1
                                msg = (raw.decode("utf-8", "replace")
                                       if isinstance(raw, (bytes, bytearray)) else str(raw))
                                msg = msg[:_OUTPUT_LINE_CAP]
                                fire, last_state = should_fire(
                                    fire_when, msg, changed=True, last_state=last_state
                                )
                                if fire and len(buffer) < _BUFFER_LINE_CAP:
                                    buffer.append(msg)
                            if buffer and (now - last_fire) >= debounce:
                                await self._fire(monitor_id, user_id, conversation_id,
                                                 description, url, fire_when, buffer)
                                buffer.clear()
                                last_fire = now
                    # Inner `async with` exited cleanly (server closed). Loop to
                    # reconnect if there's still time.
                    if _remaining() <= 0:
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Connection error/drop → backoff and retry until deadline.
                    rem = _remaining()
                    if rem <= 0:
                        ended_reason = "window ended"
                        break
                    logger.info("websocket_monitor_reconnect", monitor_id=monitor_id,
                                error=str(exc)[:160], backoff=backoff)
                    await asyncio.sleep(min(backoff, max(0.1, rem)))
                    backoff = min(backoff * 2, 30.0)
        except asyncio.CancelledError:
            ended_reason = "cancelled"
            raise
        except Exception as exc:
            ended_reason = f"error: {exc}"
            logger.warning("websocket_monitor_run_failed", monitor_id=monitor_id, error=str(exc))
        finally:
            self._tasks.pop(monitor_id, None)
            if buffer and ended_reason != "cancelled":
                try:
                    await self._fire(monitor_id, user_id, conversation_id,
                                     description, url, fire_when, buffer)
                except Exception:
                    pass
            if ended_reason != "cancelled":
                await self._finalize(monitor_id, user_id, conversation_id,
                                     description, url, msgs, ended_reason)

    @staticmethod
    async def _kill(proc: Optional[asyncio.subprocess.Process]) -> None:
        """Kill the subprocess's whole process group (the shell AND its
        pipeline children like `tail`), SIGTERM then SIGKILL."""
        if proc is None or proc.returncode is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=_GRACE_KILL_SECONDS)
        except asyncio.TimeoutError:
            try:
                if pgid is not None:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    proc.kill()
            except Exception:
                pass
        except Exception:
            pass

    # ─── Delivery + persistence ─────────────────────────────────────────────
    async def _fire(self, monitor_id, user_id, conversation_id, description,
                    command, fire_when, lines: List[str]) -> None:
        snippet = "\n".join(lines)[:_FIRE_SNIPPET_CAP]
        n = len(lines)
        prompt = (
            f"[stream:{monitor_id}] '{description}' matched {n} message(s).\n"
            f"Source: `{command}`\n"
            f"(condition: {fire_when})\n\n"
            f"Matched content:\n```\n{snippet}\n```"
        )
        # Persist counters first so fire_count is accurate even if inject fails.
        await self._bump(monitor_id, fired=True, lines=n)
        try:
            from app.services.followup_scheduler import followup_scheduler
            await followup_scheduler._inject_prompt(user_id, conversation_id, prompt)
        except Exception as exc:
            logger.warning("stream_monitor_inject_failed", user_id=user_id, error=str(exc))

    async def _bump(self, monitor_id: str, *, fired: bool = False, lines: int = 0) -> None:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select
        async with async_session_maker() as session:
            row = (await session.execute(
                select(Row).where(Row.monitor_id == monitor_id)
            )).scalar_one_or_none()
            if not row:
                return
            row.last_check_at = datetime.utcnow()
            if lines:
                row.check_count += lines  # check_count = lines read/matched
            if fired:
                row.fire_count += 1
                row.change_count += 1
                row.last_changed_at = datetime.utcnow()
            await session.commit()

    async def _finalize(self, monitor_id, user_id, conversation_id,
                        description, command, lines_read, reason) -> None:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select
        async with async_session_maker() as session:
            row = (await session.execute(
                select(Row).where(Row.monitor_id == monitor_id)
            )).scalar_one_or_none()
            if row:
                row.is_active = False
                row.last_error = None if reason == "window ended" else reason
                await session.commit()
        logger.info("stream_monitor_ended", monitor_id=monitor_id, reason=reason)
        prompt = (
            f"[stream:{monitor_id}] The watch for '{description}' has ended ({reason}).\n"
            f"Source: `{command}`\n"
            f"Messages/lines read: {lines_read}.\n\n"
            f"Please give me a one-line summary of how the watch went."
        )
        try:
            from app.services.followup_scheduler import followup_scheduler
            await followup_scheduler._inject_prompt(user_id, conversation_id, prompt)
        except Exception as exc:
            logger.warning("stream_monitor_finalize_inject_failed", user_id=user_id, error=str(exc))


# Module-global singleton; lifespan handler owns start/stop.
stream_monitor_service = StreamMonitorService()
