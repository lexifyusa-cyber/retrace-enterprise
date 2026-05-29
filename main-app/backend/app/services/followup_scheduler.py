"""
Followup Scheduler Service — fires agent-scheduled future turns.

Sibling to AutomationScheduler. Where that one runs admin-approved SOP
automation steps deterministically (no LLM), THIS one re-enters the live
agent: it POSTs a stored prompt as a user-role message into the user's
OpenCode session, just as if the user had typed it. The agent then runs a
normal turn — that turn's events stream over the regular SSE channel, so a
connected browser tab sees the new turn appear in real time; if no tab is
attached, OC's session persists everything and the next conversation refetch
picks it up.

The mechanism is the retrace analog of Claude Code's ScheduleWakeup /
CronCreate / RemoteTrigger primitives: the harness owns time, the LLM owns
instructions, and a stored prompt is replayed verbatim on each fire.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import httpx
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = structlog.get_logger()


# How long we drain OC's SSE stream after firing the message before giving up.
# Real agent turns are typically <60s; we cap higher because a scheduled
# follow-up may chain into a multi-step task.
_DRAIN_TIMEOUT_SECONDS = 300


class FollowupScheduler:
    """Manages scheduled agent follow-ups (Option B).

    Lifecycle is symmetric with AutomationScheduler — start() in lifespan,
    stop() at shutdown. Existing schedules are reloaded from DB on start so
    a backend restart doesn't drop pending follow-ups.
    """

    def __init__(self) -> None:
        # All trigger times are UTC. APScheduler interprets naive datetimes
        # as the scheduler's `timezone`, so an explicit UTC pin here lets us
        # pass naive-but-conceptually-UTC datetimes without 5+ hour drift on
        # non-UTC hosts (the trial box runs CDT locally; Fly runs UTC).
        self.scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self.scheduler.start()
        self._running = True
        logger.info("followup_scheduler_started")
        await self._load_existing()

    async def stop(self) -> None:
        if self._running:
            self.scheduler.shutdown(wait=False)
            self._running = False
            logger.info("followup_scheduler_stopped")

    # ─── DB persistence ─────────────────────────────────────────────────────
    async def _load_existing(self) -> None:
        """Rehydrate every active follow-up from DB into the scheduler."""
        try:
            from app.db.database import async_session_maker
            from app.models.scheduled_followup import ScheduledFollowup
            from sqlalchemy import select

            async with async_session_maker() as session:
                result = await session.execute(
                    select(ScheduledFollowup).where(ScheduledFollowup.is_active == True)
                )
                rows = result.scalars().all()
                added = 0
                pruned = 0
                for row in rows:
                    # One-shot whose run_at is already in the past gets
                    # auto-deactivated rather than firing late on restart.
                    if row.trigger_type == "once":
                        run_at = self._parse_iso(row.trigger_config.get("run_at"))
                        if run_at and run_at <= datetime.now(timezone.utc):
                            row.is_active = False
                            row.last_error = "expired before scheduler restart"
                            pruned += 1
                            continue
                    try:
                        self._register_job(row)
                        added += 1
                    except Exception as exc:
                        logger.warning("followup_rehydrate_failed", followup_id=row.followup_id, error=str(exc))
                if pruned:
                    await session.commit()
                logger.info("followup_scheduler_loaded", active=added, expired=pruned)
        except Exception as exc:
            logger.warning("followup_scheduler_load_failed", error=str(exc))

    @staticmethod
    def _parse_iso(value: Optional[str]) -> Optional[datetime]:
        """Parse an ISO-8601 timestamp into a UTC tz-aware datetime.

        Inputs may carry an explicit UTC marker ('Z' or '+00:00'), a different
        offset, or no tz at all (legacy rows written before we standardised
        on UTC). Treat naive as UTC because all our writers are UTC.
        """
        if not value:
            return None
        try:
            normalised = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalised)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _build_trigger(self, trigger_type: str, config: Dict[str, Any]):
        """Translate our (trigger_type, config) shape into an APScheduler trigger.

        All times are UTC. DateTrigger / CronTrigger are pinned to UTC so the
        scheduler doesn't interpret naive datetimes as local time (the bug
        that caused fires to be set 5+ hours in the future on the CDT dev box).
        """
        if trigger_type == "once":
            run_at = self._parse_iso(config.get("run_at"))
            if run_at is None:
                run_at = datetime.now(timezone.utc) + timedelta(seconds=60)
            return DateTrigger(run_date=run_at, timezone=timezone.utc)
        if trigger_type == "interval":
            every = int(config.get("every", 1))
            unit = (config.get("unit") or "minutes").lower()
            kw = {unit: every}
            return IntervalTrigger(timezone=timezone.utc, **kw)
        if trigger_type == "cron":
            expr = (config.get("expression") or "").split()
            if len(expr) >= 5:
                return CronTrigger(
                    minute=expr[0], hour=expr[1], day=expr[2],
                    month=expr[3], day_of_week=expr[4],
                    timezone=timezone.utc,
                )
        raise ValueError(f"unsupported trigger_type={trigger_type!r}")

    # ─── Public API (called from internal HTTP endpoints) ───────────────────
    async def add(
        self,
        user_id: str,
        conversation_id: Optional[str],
        prompt: str,
        trigger_type: str,
        trigger_config: Dict[str, Any],
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist + register a new scheduled follow-up. Returns the row dict."""
        from app.db.database import async_session_maker
        from app.models.scheduled_followup import ScheduledFollowup

        # Validate trigger early so we surface bad input to the agent before
        # writing to DB.
        self._build_trigger(trigger_type, trigger_config)

        async with async_session_maker() as session:
            row = ScheduledFollowup(
                followup_id=str(uuid4()),
                user_id=user_id,
                conversation_id=conversation_id,
                prompt=prompt,
                trigger_type=trigger_type,
                trigger_config=trigger_config,
                description=description,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            payload = row.to_dict()

        # Detach row from session (since we're about to use it in a callback).
        # We register the job by id — the handler reloads the row from DB at
        # fire time anyway, so the in-memory row going stale here is fine.
        self._register_job_by_id(payload["followup_id"])
        logger.info("followup_added", followup_id=payload["followup_id"], trigger_type=trigger_type)
        return payload

    async def cancel(self, followup_id: str, user_id: str) -> bool:
        """Cancel a scheduled follow-up (user-scoped). Returns whether anything changed."""
        from app.db.database import async_session_maker
        from app.models.scheduled_followup import ScheduledFollowup
        from sqlalchemy import select

        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledFollowup).where(
                    ScheduledFollowup.followup_id == followup_id,
                    ScheduledFollowup.user_id == user_id,
                )
            )
            row = result.scalar_one_or_none()
            if not row or not row.is_active:
                return False
            row.is_active = False
            await session.commit()

        try:
            self.scheduler.remove_job(self._job_id(followup_id))
        except Exception:
            pass
        logger.info("followup_cancelled", followup_id=followup_id)
        return True

    async def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        from app.db.database import async_session_maker
        from app.models.scheduled_followup import ScheduledFollowup
        from sqlalchemy import select

        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledFollowup)
                .where(ScheduledFollowup.user_id == user_id, ScheduledFollowup.is_active == True)
                .order_by(ScheduledFollowup.created_at.desc())
            )
            return [r.to_dict() for r in result.scalars().all()]

    # ─── Job registration / execution ───────────────────────────────────────
    @staticmethod
    def _job_id(followup_id: str) -> str:
        return f"followup_{followup_id}"

    def _register_job(self, row) -> None:
        trigger = self._build_trigger(row.trigger_type, row.trigger_config)
        self.scheduler.add_job(
            self._fire,
            trigger=trigger,
            id=self._job_id(row.followup_id),
            args=[row.followup_id],
            replace_existing=True,
            misfire_grace_time=300,
        )

    def _register_job_by_id(self, followup_id: str) -> None:
        # Used after add() — we already have the row payload, but reloading
        # from DB keeps the registration site symmetric with rehydrate.
        async def _go():
            from app.db.database import async_session_maker
            from app.models.scheduled_followup import ScheduledFollowup
            from sqlalchemy import select

            async with async_session_maker() as session:
                result = await session.execute(
                    select(ScheduledFollowup).where(ScheduledFollowup.followup_id == followup_id)
                )
                row = result.scalar_one_or_none()
                if row and row.is_active:
                    self._register_job(row)

        # Schedule the registration on the running loop without awaiting from a
        # sync context.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_go())
        except RuntimeError:
            asyncio.run(_go())

    async def _fire(self, followup_id: str) -> None:
        """Job handler — re-read the row, inject the prompt into OC, drain SSE."""
        from app.db.database import async_session_maker
        from app.models.scheduled_followup import ScheduledFollowup
        from sqlalchemy import select

        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledFollowup).where(ScheduledFollowup.followup_id == followup_id)
            )
            row = result.scalar_one_or_none()
            if not row or not row.is_active:
                # Row was cancelled or pruned between the trigger and now.
                try:
                    self.scheduler.remove_job(self._job_id(followup_id))
                except Exception:
                    pass
                return
            row.fire_count += 1
            row.last_fired_at = datetime.utcnow()
            row.last_error = None
            user_id = row.user_id
            conversation_id = row.conversation_id
            prompt = row.prompt
            description = row.description or ""
            trigger_type = row.trigger_type
            trigger_config = row.trigger_config or {}
            # Auto-deactivate logic. Two cases:
            #   1. `once` triggers — they fire exactly one time; flip is_active
            #      False so list_followups doesn't show retired rows.
            #   2. intervals/crons with `stop_after` in trigger_config — either
            #      `stop_after_fires` (cap by count) or `stop_after` (UTC ISO
            #      deadline). When either condition is met, deactivate and
            #      remove the APScheduler job so it stops firing.
            should_deactivate = False
            if trigger_type == "once":
                should_deactivate = True
            else:
                cap_fires = trigger_config.get("stop_after_fires")
                if isinstance(cap_fires, int) and row.fire_count >= cap_fires:
                    should_deactivate = True
                cap_iso = trigger_config.get("stop_after")
                cap_dt = self._parse_iso(cap_iso) if cap_iso else None
                if cap_dt and datetime.now(timezone.utc) >= cap_dt:
                    should_deactivate = True
            if should_deactivate:
                row.is_active = False
            await session.commit()

        if should_deactivate:
            try:
                self.scheduler.remove_job(self._job_id(followup_id))
            except Exception:
                pass
            logger.info("followup_auto_retired", followup_id=followup_id, trigger_type=trigger_type)

        logger.info(
            "followup_fire",
            followup_id=followup_id, user_id=user_id, conv_id=conversation_id,
            prompt_len=len(prompt), description=description[:80],
        )

        try:
            await self._inject_prompt(user_id, conversation_id, prompt)
        except Exception as exc:
            logger.warning("followup_fire_failed", followup_id=followup_id, error=str(exc))
            # Persist the error so the agent can surface it via list_followups.
            try:
                async with async_session_maker() as session:
                    result = await session.execute(
                        select(ScheduledFollowup).where(ScheduledFollowup.followup_id == followup_id)
                    )
                    row = result.scalar_one_or_none()
                    if row:
                        row.last_error = str(exc)[:1000]
                        await session.commit()
            except Exception:
                pass

    async def _inject_prompt(
        self,
        user_id: str,
        conversation_id: Optional[str],
        prompt: str,
    ) -> None:
        """POST the prompt as a user-role message into the user's OC session,
        then drain the SSE so the resulting turn gets persisted to retrace's DB
        via the existing post-stream sync hook.
        """
        from app.services.opencode_manager import opencode_manager
        from app.services.session_bridge import (
            find_opencode_session_id, sync_messages_from_opencode,
        )

        if not conversation_id:
            raise RuntimeError("conversation_id required for fire (anon mode not implemented yet)")

        port = opencode_manager.get_port(user_id)
        if not port:
            # Attempt a cold-start. start_for_user is idempotent.
            try:
                port = await opencode_manager.start_for_user(user_id=user_id)
            except Exception as exc:
                raise RuntimeError(f"OC not running and start_for_user failed: {exc}")
        if not port:
            raise RuntimeError("OC port not available")

        oc_session_id = await find_opencode_session_id(conversation_id)
        if not oc_session_id:
            raise RuntimeError(f"no OC session linked to conversation {conversation_id}")

        # Verify the session is still alive on OC. If not, the dead-session
        # healer (cdb3362) is the frontend's job — for a scheduled fire we
        # surface the error and let the agent's next user-driven turn heal.
        async with httpx.AsyncClient(timeout=10) as client:
            check = await client.get(f"http://127.0.0.1:{port}/session/{oc_session_id}")
            if check.status_code == 404:
                raise RuntimeError(f"OC session {oc_session_id} no longer exists (404)")

        # Subscribe to /global/event BEFORE posting — same ordering retrace's
        # user-facing send_message uses (agent.py:1004-1009). If we POSTed
        # first, message.updated events for this turn could fire before our
        # subscriber attaches, and we'd miss the finish=stop marker, leaving
        # the drain to time out instead of returning promptly.
        try:
            await asyncio.wait_for(
                self._post_and_drain(port, oc_session_id, prompt, conversation_id),
                timeout=_DRAIN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("followup_drain_timeout", oc_session_id=oc_session_id)

        # Persist whatever OC has accumulated for this session, regardless of
        # whether we saw the explicit "done" event. Idempotent on duplicate
        # parts (sync_messages_from_opencode uses part IDs as primary keys).
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                msgs_resp = await client.get(
                    f"http://127.0.0.1:{port}/session/{oc_session_id}/message"
                )
                if msgs_resp.status_code == 200:
                    await sync_messages_from_opencode(
                        oc_session_id,
                        msgs_resp.json(),
                        wait_for_oc_title_seconds=2.0,
                    )
        except Exception as exc:
            logger.warning("followup_post_sync_failed", error=str(exc))

    async def _post_and_drain(
        self,
        port: int,
        session_id: str,
        prompt: str,
        conversation_id: Optional[str] = None,
    ) -> None:
        """Open OC's /global/event SSE FIRST, then POST the prompt to
        /session/<sid>/prompt_async, then read events until we see a
        message.updated with finish='stop' for this session. Order matches
        retrace's user-facing send_message (subscribe → POST → drain).

        When `conversation_id` is provided, every event line we read for
        THIS session also gets fanned out via `conv_event_publisher` so
        any open browser tab subscribed to that conversation can render
        the fired turn live (synthetic user bubble + agent streaming).
        Typed turns don't broadcast — opencode_send_message has its own
        SSE response stream; duplicate publish would deliver events twice.
        """
        import httpx as _httpx
        from app.services.conv_event_publisher import conv_event_publisher

        async with _httpx.AsyncClient(timeout=_httpx.Timeout(_DRAIN_TIMEOUT_SECONDS, connect=10.0)) as client:
            async with client.stream(
                "GET", f"http://127.0.0.1:{port}/global/event",
                headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
            ) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"/global/event subscribe failed: {resp.status_code}")
                # Fire the prompt now that we're subscribed. retrace's user-
                # facing path POSTs to /session/<sid>/prompt_async with shape
                # {"parts": [{"type":"text","text":...}]}; we mirror it +
                # mark `synthetic: true` so the frontend can render the
                # resulting user bubble distinctly (system-injected, not
                # typed). Same flag OC's own task tool uses for its
                # completion-notification injects (task.ts:248-251).
                post_resp = await client.post(
                    f"http://127.0.0.1:{port}/session/{session_id}/prompt_async",
                    json={"parts": [{"type": "text", "text": prompt, "synthetic": True}]},
                )
                if post_resp.status_code not in (200, 202, 204):
                    raise RuntimeError(
                        f"prompt_async returned {post_resp.status_code}: {post_resp.text[:200]}"
                    )
                logger.info(
                    "followup_prompt_posted",
                    oc_session_id=session_id, status=post_resp.status_code,
                )
                # Drain until the assistant message for this turn lands its
                # final message.updated with finish="stop". Along the way,
                # fan out events to subscribers of this conversation_id so
                # the open browser tab sees the fired turn live.
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        payload = json.loads(data_str)
                    except Exception:
                        continue
                    inner = payload.get("payload", payload)
                    props = inner.get("properties", {}) or {}
                    info = props.get("info", {}) or {}
                    evt_sid = info.get("sessionID") or props.get("sessionID", "")
                    # Filter: only forward events that belong to this turn's
                    # OC session. Other sessions on the same daemon (e.g. a
                    # second tab's chat) shouldn't bleed into this tab.
                    if evt_sid and evt_sid != session_id:
                        continue
                    if conversation_id:
                        try:
                            await conv_event_publisher.publish(conversation_id, line)
                        except Exception as exc:
                            logger.warning("conv_event_publish_failed", error=str(exc))
                    if inner.get("type") == "message.updated" and info.get("finish") == "stop":
                        return


# Module-global singleton; lifespan handler owns start/stop.
followup_scheduler = FollowupScheduler()
