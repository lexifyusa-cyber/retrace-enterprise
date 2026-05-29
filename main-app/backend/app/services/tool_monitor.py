"""
Tool Monitor Service — the unified change-monitor backend.

Supersedes url_monitor / command_monitor / browser_monitor. One service,
one APScheduler, one `_check` tick. The only thing that varies per monitor
is `_invoke_target` (how we get this tick's output) and `fire_when` (whether
the output warrants waking the agent).

Invocation paths (target_kind):
  url      — httpx GET + HTML-strip          (native, this phase)
  command  — create_subprocess_shell stdout   (native, this phase)
  builtin  — call a retrace MCP tool's backend logic (Phase 3; browser=read_page)
  mcp      — mcp_client_manager.call_tool      (Phase 4; user-installed MCPs)

Firing is decided by monitor_predicate.should_fire — see that module for the
predicate language and per-kind firing policy. The pipeline (hash, diff,
predicate, inject, expire, auto-deactivate) is identical regardless of how
the output was obtained.

LLM cost during quiet windows is zero: invoke + hash + predicate is pure
Python. The agent only wakes when should_fire says so.
"""

import asyncio
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.services.monitor_predicate import should_fire

logger = structlog.get_logger()


# Native-path timeouts / caps (mirrors the legacy per-kind services).
_URL_FETCH_TIMEOUT = 20.0
_CMD_RUN_TIMEOUT = 60.0
_OUTPUT_CAP = 256 * 1024  # 256 KB captured before hashing
_DIFF_SNIPPET_LEN = 1500  # how much output we hand the LLM on a fire

# Minimum cadence per kind. Native paths and in-process builtin tools
# (read_page is a single page.eval) can poll fast; user-installed MCP
# tools have spawn/reconnect overhead so they're floored higher.
_MIN_EVERY = {"url": 5, "command": 5, "builtin": 5, "mcp": 30}

# Builtin retrace tools the monitor is allowed to invoke. Each maps the
# tool name to an async invoker(conversation_id, args) -> (text, url|None).
# Keeping this an explicit allowlist (rather than calling arbitrary backend
# code) keeps the surface auditable; add an entry to make a new builtin
# tool monitorable.
_BUILTIN_TOOLS = ("read_page",)


class ToolMonitorService:
    """Manages periodic tool-output-diff watches with predicate firing."""

    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self.scheduler.start()
        self._running = True
        logger.info("tool_monitor_service_started")
        await self._load_existing()

    async def stop(self) -> None:
        if self._running:
            self.scheduler.shutdown(wait=False)
            self._running = False
            logger.info("tool_monitor_service_stopped")

    async def _load_existing(self) -> None:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select

        try:
            async with async_session_maker() as session:
                # target_kind="stream" rows are owned by StreamMonitorService
                # (persistent tasks), not this poll-based service.
                result = await session.execute(
                    select(Row).where(Row.is_active == True, Row.target_kind != "stream")
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
                    try:
                        self._register_job(row)
                        added += 1
                    except Exception as exc:
                        logger.warning("tool_monitor_rehydrate_failed",
                                       monitor_id=row.monitor_id, error=str(exc))
                if pruned:
                    await session.commit()
                logger.info("tool_monitor_loaded", active=added, expired=pruned)
        except Exception as exc:
            logger.warning("tool_monitor_load_failed", error=str(exc))

    # ─── Public API ─────────────────────────────────────────────────────────
    async def add(
        self,
        user_id: str,
        conversation_id: str,
        target_kind: str,
        target_json: Dict[str, Any],
        every_seconds: int,
        duration_seconds: int,
        prompt_on_change: str,
        fire_when: str = "changed",
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row

        if target_kind not in _MIN_EVERY:
            raise ValueError(
                f"target_kind must be one of {sorted(_MIN_EVERY)}, got {target_kind!r}"
            )
        min_every = _MIN_EVERY[target_kind]
        if every_seconds < min_every:
            raise ValueError(
                f"every_seconds must be >= {min_every} for {target_kind} targets"
            )
        if duration_seconds < every_seconds:
            raise ValueError("duration_seconds must be >= every_seconds")

        # For mcp targets, resolve the caller-supplied `extension` (name or id)
        # into a canonical extension_id so the stored target is unambiguous.
        if target_kind == "mcp":
            ident = target_json.get("extension_id") or target_json.get("extension")
            if not ident:
                raise ValueError("mcp target requires target_json.extension (name or id)")
            ext_id, ext_name = await self._resolve_mcp_extension(user_id, ident)
            target_json = {
                "extension_id": ext_id,
                "extension_name": ext_name,
                "tool": target_json.get("tool"),
                "args": target_json.get("args") or {},
            }

        self._validate_target(target_kind, target_json)

        async with async_session_maker() as session:
            row = Row(
                user_id=user_id,
                conversation_id=conversation_id,
                target_kind=target_kind,
                target_json=target_json,
                every_seconds=every_seconds,
                expires_at=datetime.utcnow() + timedelta(seconds=duration_seconds),
                fire_when=(fire_when or "changed").strip(),
                prompt_on_change=prompt_on_change,
                description=description,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            payload = row.to_dict()

        self._register_job_by_id(payload["monitor_id"])
        logger.info("tool_monitor_added", monitor_id=payload["monitor_id"],
                    target_kind=target_kind, every_seconds=every_seconds,
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
        try:
            self.scheduler.remove_job(self._job_id(monitor_id))
        except Exception:
            pass
        logger.info("tool_monitor_cancelled", monitor_id=monitor_id)
        return True

    async def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select

        async with async_session_maker() as session:
            result = await session.execute(
                select(Row)
                .where(Row.user_id == user_id, Row.is_active == True)
                .order_by(Row.created_at.desc())
            )
            return [r.to_dict() for r in result.scalars().all()]

    @staticmethod
    async def _resolve_mcp_extension(user_id: str, ident: str):
        """Resolve a user-supplied extension identifier (UUID or case-insensitive
        name) to (extension_id, name) among the user's enabled extensions.
        Raises ValueError listing the available ones if not found."""
        from sqlalchemy import select as sa_select
        from app.db.database import async_session_maker
        from app.models.mcp_extension import McpExtension
        from app.models.user_extension import UserExtension

        async with async_session_maker() as session:
            rows = (await session.execute(
                sa_select(McpExtension.extension_id, McpExtension.name)
                .join(UserExtension, UserExtension.extension_id == McpExtension.extension_id)
                .where(UserExtension.user_id == user_id)
                .where(UserExtension.is_enabled == True)
                .where(McpExtension.is_enabled == True)
            )).all()
        installed = [(eid, nm) for eid, nm in rows]
        ident_l = (ident or "").strip().lower()
        for eid, nm in installed:
            if eid == ident or (nm or "").lower() == ident_l:
                return eid, nm
        avail = ", ".join(nm for _, nm in installed) or "(none installed)"
        raise ValueError(
            f"MCP extension {ident!r} not found among your installed extensions. "
            f"Available: {avail}"
        )

    # ─── Target validation ──────────────────────────────────────────────────
    @staticmethod
    def _validate_target(kind: str, target: Dict[str, Any]) -> None:
        if kind == "url":
            url = (target or {}).get("url", "")
            if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
                raise ValueError("url target requires target_json.url starting http(s)://")
        elif kind == "command":
            if not (target or {}).get("command", "").strip():
                raise ValueError("command target requires non-empty target_json.command")
        elif kind == "builtin":
            tool = (target or {}).get("tool")
            if not tool:
                raise ValueError("builtin target requires target_json.tool")
            if tool not in _BUILTIN_TOOLS:
                raise ValueError(
                    f"builtin tool {tool!r} not monitorable (supported: {', '.join(_BUILTIN_TOOLS)})"
                )
        elif kind == "mcp":
            if not ((target or {}).get("extension_id") and (target or {}).get("tool")):
                raise ValueError("mcp target requires target_json.extension_id and .tool")

    # ─── Job lifecycle ──────────────────────────────────────────────────────
    @staticmethod
    def _job_id(monitor_id: str) -> str:
        return f"tool_monitor_{monitor_id}"

    def _register_job(self, row) -> None:
        trigger = IntervalTrigger(seconds=row.every_seconds, timezone=timezone.utc)
        self.scheduler.add_job(
            self._check, trigger=trigger, id=self._job_id(row.monitor_id),
            args=[row.monitor_id], replace_existing=True,
            misfire_grace_time=row.every_seconds,
            max_instances=1,  # skip a tick if the previous one is still running
        )

    def _register_job_by_id(self, monitor_id: str) -> None:
        async def _go():
            from app.db.database import async_session_maker
            from app.models.tool_monitor import ToolMonitor as Row
            from sqlalchemy import select
            async with async_session_maker() as session:
                result = await session.execute(select(Row).where(Row.monitor_id == monitor_id))
                row = result.scalar_one_or_none()
                if row and row.is_active:
                    self._register_job(row)
        try:
            asyncio.get_running_loop().create_task(_go())
        except RuntimeError:
            asyncio.run(_go())

    # ─── The unified tick ───────────────────────────────────────────────────
    async def _check(self, monitor_id: str) -> None:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select

        async with async_session_maker() as session:
            result = await session.execute(select(Row).where(Row.monitor_id == monitor_id))
            row = result.scalar_one_or_none()
            if not row or not row.is_active:
                try:
                    self.scheduler.remove_job(self._job_id(monitor_id))
                except Exception:
                    pass
                return
            user_id = row.user_id
            conversation_id = row.conversation_id
            target_kind = row.target_kind
            target_json = dict(row.target_json or {})
            fire_when = row.fire_when or "changed"
            prompt_on_change = row.prompt_on_change
            description = row.description or self._default_desc(target_kind, target_json)
            previous_hash = row.last_content_hash
            last_predicate_state = row.last_predicate_state
            last_changed_at = row.last_changed_at
            expires_at = row.expires_at

        # 1) Invoke the target (outside the DB transaction).
        output: Optional[str] = None
        current_url: Optional[str] = None
        invoke_error: Optional[str] = None
        try:
            output, current_url = await self._invoke_target(
                target_kind, target_json, conversation_id, user_id
            )
        except Exception as exc:
            invoke_error = str(exc)[:500]
            logger.warning("tool_monitor_invoke_failed",
                           monitor_id=monitor_id, kind=target_kind, error=invoke_error)

        now = datetime.utcnow()

        # 2) On invoke error: record + skip predicate/fire this tick.
        if invoke_error is not None:
            await self._persist_tick(monitor_id, now, error=invoke_error)
            if now >= expires_at:
                await self._expire(monitor_id, user_id, conversation_id, description)
            return

        # 3) Hash + diff.
        new_hash = hashlib.sha256(output.encode("utf-8", "replace")).hexdigest() if output else ""
        baseline_set = previous_hash == "" and new_hash != ""
        changed = bool(previous_hash) and bool(new_hash) and new_hash != previous_hash
        seconds_since_change = (
            (now - last_changed_at).total_seconds() if last_changed_at else None
        )

        # 4) Predicate decides firing. Baseline tick never fires (we just
        #    establish the hash) EXCEPT predicates that are level-meaningful
        #    on first sight are still allowed by should_fire — but we suppress
        #    on the very first baseline to avoid a spurious "changed" fire.
        if baseline_set:
            fire, new_state = False, last_predicate_state
        else:
            fire, new_state = should_fire(
                fire_when, output or "",
                changed=changed,
                seconds_since_change=seconds_since_change,
                last_state=last_predicate_state,
            )

        # 5) Persist tick outcome.
        await self._persist_tick(
            monitor_id, now,
            new_hash=(new_hash if (new_hash and (baseline_set or changed)) else None),
            changed=changed, fired=fire, new_predicate_state=new_state,
            current_url=current_url,
        )

        # 6) Expiry takes precedence over a fire on the same tick.
        if now >= expires_at:
            await self._expire(monitor_id, user_id, conversation_id, description)
            return

        if fire:
            preview = (output or "")[:_DIFF_SNIPPET_LEN]
            if output and len(output) > _DIFF_SNIPPET_LEN:
                preview += f"\n... [truncated, {len(output) - _DIFF_SNIPPET_LEN} more chars]"
            await self._inject(user_id, conversation_id, self._change_text(
                description, monitor_id, target_kind, target_json,
                current_url, fire_when, prompt_on_change, preview,
            ))

    async def _persist_tick(
        self, monitor_id: str, now: datetime, *,
        new_hash: Optional[str] = None, changed: bool = False,
        fired: bool = False, new_predicate_state: Optional[bool] = None,
        current_url: Optional[str] = None, error: Optional[str] = None,
    ) -> None:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select
        async with async_session_maker() as session:
            result = await session.execute(select(Row).where(Row.monitor_id == monitor_id))
            row = result.scalar_one_or_none()
            if not row:
                return
            row.check_count += 1
            row.last_check_at = now
            row.last_error = error
            if current_url:
                row.last_url = current_url
            if new_hash is not None:
                row.last_content_hash = new_hash
            if changed:
                row.change_count += 1
                row.last_changed_at = now
            if new_predicate_state is not None:
                row.last_predicate_state = new_predicate_state
            if fired:
                row.fire_count += 1
            await session.commit()

    async def _expire(self, monitor_id: str, user_id: str,
                      conversation_id: str, description: str) -> None:
        from app.db.database import async_session_maker
        from app.models.tool_monitor import ToolMonitor as Row
        from sqlalchemy import select
        checks = changes = 0
        async with async_session_maker() as session:
            result = await session.execute(select(Row).where(Row.monitor_id == monitor_id))
            row = result.scalar_one_or_none()
            if row:
                row.is_active = False
                checks, changes = row.check_count, row.change_count
                await session.commit()
        try:
            self.scheduler.remove_job(self._job_id(monitor_id))
        except Exception:
            pass
        logger.info("tool_monitor_window_ended", monitor_id=monitor_id)
        await self._inject(user_id, conversation_id, self._window_ended_text(
            description, monitor_id, checks, changes))

    async def _inject(self, user_id: str, conversation_id: str, prompt: str) -> None:
        try:
            from app.services.followup_scheduler import followup_scheduler
            await followup_scheduler._inject_prompt(user_id, conversation_id, prompt)
        except Exception as exc:
            logger.warning("tool_monitor_inject_failed", user_id=user_id, error=str(exc))

    # ─── Target invocation dispatch ─────────────────────────────────────────
    async def _invoke_target(
        self, kind: str, target: Dict[str, Any], conversation_id: str, user_id: str,
    ) -> Tuple[str, Optional[str]]:
        """Return (output_text, current_url_or_none). Raises on failure."""
        if kind == "url":
            return await self._fetch_url(target["url"], target.get("selector"))
        if kind == "command":
            return await self._run_command(target["command"]), None
        if kind == "builtin":
            return await self._invoke_builtin(
                target.get("tool"), target.get("args") or {}, conversation_id
            )
        if kind == "mcp":
            return await self._invoke_mcp(
                target.get("extension_id"), target.get("tool"),
                target.get("args") or {}, user_id,
            )
        raise ValueError(f"unknown target kind: {kind!r}")

    async def _invoke_mcp(
        self, extension_id: Optional[str], tool: Optional[str],
        args: Dict[str, Any], user_id: str,
    ) -> Tuple[str, Optional[str]]:
        """Invoke a user-installed MCP tool via the backend's MCP client.

        call_tool returns the tool's text output on success, or a JSON
        '{"error": ...}' string on failure. If the connection isn't running
        (idle-timed-out or never started this process), cold-start it from
        DB config and retry once.
        """
        import json as _json
        from app.services.mcp_client_service import mcp_client_manager

        if not (extension_id and tool):
            raise RuntimeError("mcp target missing extension_id or tool")

        raw = await mcp_client_manager.call_tool(extension_id, tool, args, user_id=user_id)
        err = self._call_tool_error(raw)
        if err and "not running" in err.lower():
            await self._ensure_mcp_started(extension_id, user_id)
            raw = await mcp_client_manager.call_tool(extension_id, tool, args, user_id=user_id)
            err = self._call_tool_error(raw)
        if err:
            raise RuntimeError(err)
        return raw or "", None

    @staticmethod
    def _call_tool_error(raw: Optional[str]) -> Optional[str]:
        """call_tool returns plain text on success but a JSON {"error":...}
        string on failure. Detect the failure shape. (A tool whose real
        output is itself {"error":...} JSON would be misread — acceptable
        edge case; most monitored tools return data.)"""
        if not raw:
            return None
        s = raw.strip()
        if s.startswith("{") and '"error"' in s:
            try:
                import json as _json
                obj = _json.loads(s)
                if isinstance(obj, dict) and "error" in obj:
                    return str(obj["error"])
            except Exception:
                pass
        return None

    async def _ensure_mcp_started(self, extension_id: str, user_id: str) -> None:
        """Cold-start a user's MCP extension from DB config (idempotent —
        start_extension pings + reuses a live connection). Mirrors the
        agent.py warmup path for a single extension."""
        from sqlalchemy import select as sa_select
        from app.db.database import async_session_maker
        from app.models.mcp_extension import McpExtension
        from app.models.user_extension import UserExtension
        from app.models.user_session import UserSession
        from app.services.mcp_client_service import mcp_client_manager

        async with async_session_maker() as session:
            row = (await session.execute(
                sa_select(UserExtension, McpExtension)
                .join(McpExtension, UserExtension.extension_id == McpExtension.extension_id)
                .where(UserExtension.user_id == user_id)
                .where(UserExtension.extension_id == extension_id)
                .where(UserExtension.is_enabled == True)
                .where(McpExtension.is_enabled == True)
            )).first()
            if not row:
                raise RuntimeError(
                    f"MCP extension {extension_id} is not installed/enabled for this user"
                )
            user_ext, catalog_ext = row
            user_auth_token = None
            user_auth_credentials = None
            sess = (await session.execute(
                sa_select(UserSession)
                .where(UserSession.user_id == user_id)
                .where(UserSession.revoked == False)
                .order_by(UserSession.last_active_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            if sess:
                user_auth_token = sess.external_id_token or sess.external_access_token
                if sess.external_username and sess.external_password:
                    from app.core.encryption import decrypt_value
                    user_auth_credentials = {
                        "username": sess.external_username,
                        "password": decrypt_value(sess.external_password),
                    }
            tenant_id = user_ext.tenant_id or ""
            merged_env = user_ext.get_merged_env_vars(catalog_ext.get_raw_env_vars())
            merged_auth = {**(catalog_ext.auth_config_json or {}), **(user_ext.auth_config_json or {})}
            name = catalog_ext.name
            command = catalog_ext.command
            args_json = catalog_ext.args_json
            auth_type = catalog_ext.auth_type
            transport = getattr(catalog_ext, "transport", "stdio") or "stdio"
            url = getattr(catalog_ext, "url", None)

        await mcp_client_manager.start_extension(
            extension_id=extension_id, extension_name=name,
            command=command, args=args_json, env_vars=merged_env,
            auth_type=auth_type, auth_config=merged_auth,
            user_auth_token=user_auth_token or "",
            user_auth_credentials=user_auth_credentials,
            user_id=user_id, tenant_id=tenant_id,
            transport=transport, url=url,
        )

    async def _invoke_builtin(
        self, tool: Optional[str], args: Dict[str, Any], conversation_id: str,
    ) -> Tuple[str, Optional[str]]:
        """Invoke a whitelisted retrace built-in tool's backend logic directly
        (no HTTP loopback). Currently: read_page (browser DOM text)."""
        if tool == "read_page":
            from app.services.browser_manager import browser_manager
            sess = await browser_manager.get(conversation_id)
            if not sess or not sess.page:
                raise RuntimeError(
                    "No live browser session for this conversation. Open a page "
                    "via the browser tool first so the watch has something to read."
                )
            res = await sess.read_page(args.get("selector"))
            if isinstance(res, dict) and res.get("error"):
                raise RuntimeError(f"read_page failed: {res['error']}")
            text = (res or {}).get("text") or ""
            return text, (res or {}).get("url")
        raise RuntimeError(
            f"builtin tool {tool!r} is not monitorable "
            f"(supported: {', '.join(_BUILTIN_TOOLS)})"
        )

    async def _fetch_url(self, url: str, selector: Optional[str]) -> Tuple[str, Optional[str]]:
        async with httpx.AsyncClient(
            timeout=_URL_FETCH_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; retrace-monitor/1.0)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            body = resp.text or ""
        body = re.sub(r"<script\b[^>]*>.*?</script>", "", body, flags=re.S | re.I)
        body = re.sub(r"<style\b[^>]*>.*?</style>", "", body, flags=re.S | re.I)
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
        body = re.sub(r"<[^>]+>", " ", body)
        body = body.replace("&nbsp;", " ").replace("&amp;", "&")
        body = re.sub(r"\s+", " ", body).strip()
        if len(body) > _OUTPUT_CAP:
            body = body[:_OUTPUT_CAP]
        return body, url

    async def _run_command(self, command: str) -> str:
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_CMD_RUN_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise RuntimeError(f"command timed out after {_CMD_RUN_TIMEOUT}s")
        if len(stdout_bytes) > _OUTPUT_CAP:
            stdout_bytes = stdout_bytes[:_OUTPUT_CAP]
        return stdout_bytes.decode("utf-8", "replace")

    # ─── Notification text ──────────────────────────────────────────────────
    @staticmethod
    def _default_desc(kind: str, target: Dict[str, Any]) -> str:
        if kind == "url":
            return f"watch {target.get('url', '')}"
        if kind == "command":
            return f"watch `{(target.get('command') or '')[:60]}`"
        if kind == "builtin":
            return f"watch tool {target.get('tool')}"
        if kind == "mcp":
            ext = target.get("extension_name") or target.get("extension_id")
            return f"watch {ext}:{target.get('tool')}"
        return "watch"

    @staticmethod
    def _change_text(description: str, monitor_id: str, kind: str,
                     target: Dict[str, Any], url: Optional[str], fire_when: str,
                     base_prompt: str, snippet: str) -> str:
        loc = ""
        if kind == "url":
            loc = f"URL: {url or target.get('url')}\n"
        elif kind == "command":
            loc = f"Command: `{target.get('command')}`\n"
        elif kind == "builtin":
            loc = f"Tool: {target.get('tool')}\n"
        elif kind == "mcp":
            ext = target.get("extension_name") or target.get("extension_id")
            loc = f"Tool: {ext}:{target.get('tool')}\n"
        cond = "" if fire_when in ("", "changed") else f"(condition: {fire_when})\n"
        return (
            f"[monitor:{monitor_id}] '{description}' fired.\n"
            f"{loc}{cond}\n"
            f"{base_prompt}\n\n"
            f"Current output:\n```\n{snippet}\n```"
        )

    @staticmethod
    def _window_ended_text(description: str, monitor_id: str, checks: int, changes: int) -> str:
        return (
            f"[monitor:{monitor_id}] The watch window for '{description}' has ended.\n"
            f"Stats: {checks} checks performed, {changes} change(s) detected.\n\n"
            f"Please give me a one-line summary of how the watch went."
        )


# Module-global singleton; lifespan handler owns start/stop.
tool_monitor_service = ToolMonitorService()
