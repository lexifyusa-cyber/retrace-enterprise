"""
Automation Scheduler Service — APScheduler-based execution of approved automations.

Runs approved automation steps via the agent service at scheduled times.
"""

import asyncio
import json
import time
import traceback
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = structlog.get_logger()


class AutomationScheduler:
    """Manages scheduling and execution of approved automations."""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._running = False

    async def start(self):
        """Start the scheduler and load existing scheduled automations."""
        if self._running:
            return
        self.scheduler.start()
        self._running = True
        logger.info("automation_scheduler_started")

        # Load and schedule existing approved automations
        await self._load_existing_schedules()

    async def stop(self):
        """Shut down the scheduler gracefully."""
        if self._running:
            self.scheduler.shutdown(wait=False)
            self._running = False
            logger.info("automation_scheduler_stopped")

    async def _load_existing_schedules(self):
        """Load all approved automations with schedules from DB and add them."""
        try:
            from app.db.database import async_session_maker
            from app.models.sop import SOP
            from sqlalchemy import select

            async with async_session_maker() as session:
                result = await session.execute(
                    select(SOP).where(
                        SOP.status == "approved",
                        SOP.is_active == True,
                        SOP.schedule_type.isnot(None),
                    )
                )
                sops = result.scalars().all()
                for sop in sops:
                    self.add_automation_job(
                        sop_id=sop.sop_id,
                        product_id=sop.product_id,
                        schedule_type=sop.schedule_type,
                        schedule_config=sop.schedule_config or {},
                    )
                logger.info("loaded_existing_schedules", count=len(sops))
        except Exception as exc:
            logger.warning("load_schedules_failed", error=str(exc))

    def add_automation_job(
        self,
        sop_id: str,
        product_id: str,
        schedule_type: str,
        schedule_config: dict,
    ):
        """Add or replace a scheduled job for an automation."""
        job_id = f"automation_{sop_id}"

        # Remove existing job if any
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass

        trigger = self._build_trigger(schedule_type, schedule_config)
        if trigger is None:
            return

        self.scheduler.add_job(
            self._execute_automation,
            trigger=trigger,
            id=job_id,
            args=[sop_id, product_id],
            replace_existing=True,
            misfire_grace_time=300,
        )
        logger.info("automation_job_added", sop_id=sop_id, schedule_type=schedule_type)

    def remove_automation_job(self, sop_id: str):
        """Remove a scheduled job."""
        job_id = f"automation_{sop_id}"
        try:
            self.scheduler.remove_job(job_id)
            logger.info("automation_job_removed", sop_id=sop_id)
        except Exception:
            pass

    def _build_trigger(self, schedule_type: str, config: dict):
        """Build an APScheduler trigger from schedule config."""
        if schedule_type == "once":
            run_at = config.get("run_at")
            if run_at:
                dt = datetime.fromisoformat(run_at.replace("Z", "+00:00").replace("+00:00", ""))
                return DateTrigger(run_date=dt)
            return DateTrigger(run_date=datetime.utcnow() + timedelta(minutes=1))

        elif schedule_type == "interval":
            every = config.get("every", 60)
            unit = config.get("unit", "minutes")
            if unit == "minutes":
                return IntervalTrigger(minutes=every)
            elif unit == "hours":
                return IntervalTrigger(hours=every)
            elif unit == "days":
                return IntervalTrigger(days=every)

        elif schedule_type == "daily":
            time_str = config.get("time", "09:00")
            h, m = map(int, time_str.split(":"))
            return CronTrigger(hour=h, minute=m)

        elif schedule_type == "weekly":
            days = config.get("days", ["monday"])
            time_str = config.get("time", "09:00")
            h, m = map(int, time_str.split(":"))
            day_map = {
                "monday": "mon", "tuesday": "tue", "wednesday": "wed",
                "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun",
            }
            day_of_week = ",".join(day_map.get(d.lower(), d[:3]) for d in days)
            return CronTrigger(day_of_week=day_of_week, hour=h, minute=m)

        elif schedule_type == "monthly":
            day = config.get("day_of_month", 1)
            time_str = config.get("time", "09:00")
            h, m = map(int, time_str.split(":"))
            return CronTrigger(day=day, hour=h, minute=m)

        elif schedule_type == "cron":
            expr = config.get("expression", "0 9 * * 1-5")
            parts = expr.split()
            if len(parts) >= 5:
                return CronTrigger(
                    minute=parts[0], hour=parts[1], day=parts[2],
                    month=parts[3], day_of_week=parts[4],
                )

        return None

    async def _execute_automation(self, sop_id: str, product_id: str):
        """Execute an automation's steps using the agent tools."""
        from app.db.database import async_session_maker
        from app.models.sop import SOP
        from app.models.automation_run import AutomationRun
        from sqlalchemy import select

        run_id = str(uuid4())
        t0 = time.time()
        log_parts = []

        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(SOP).where(SOP.sop_id == sop_id)
                )
                sop = result.scalar_one_or_none()
                if not sop or not sop.sop_json:
                    logger.warning("automation_sop_not_found", sop_id=sop_id)
                    return

                steps = sop.sop_json.get("steps", [])
                total_steps = len(steps)

                # Create run record
                run = AutomationRun(
                    run_id=run_id,
                    sop_id=sop_id,
                    product_id=product_id,
                    trigger="scheduled",
                    status="running",
                    steps_total=total_steps,
                    steps_completed=0,
                    started_at=datetime.utcnow(),
                )
                session.add(run)
                await session.commit()

            logger.info("automation_run_started", run_id=run_id, sop_id=sop_id, steps=total_steps)

            # Execute each step
            completed = 0
            for step in steps:
                step_num = step.get("number", completed + 1)
                tool = step.get("tool", "terminal")
                command = step.get("command", "")
                action = step.get("action", "")

                log_parts.append(f"=== Step {step_num}: {action} ===")
                log_parts.append(f"Tool: {tool}")
                log_parts.append(f"Command: {command}")

                try:
                    output = await self._run_step(tool, command, product_id, step=step)
                    log_parts.append(f"Output: {output[:2000]}")
                    # Treat non-zero exit code (e.g. from terminal) as step failure
                    if "[exit code:" in output and "[exit code: 0]" not in output:
                        break
                    completed += 1
                except Exception as step_exc:
                    log_parts.append(f"Error: {str(step_exc)}")
                    log_parts.append(traceback.format_exc())
                    break

                log_parts.append("")

            duration_ms = int((time.time() - t0) * 1000)
            final_status = "completed" if completed == total_steps else "failed"

            # Update run record
            async with async_session_maker() as session:
                result = await session.execute(
                    select(AutomationRun).where(AutomationRun.run_id == run_id)
                )
                run = result.scalar_one_or_none()
                if run:
                    run.status = final_status
                    run.steps_completed = completed
                    run.output_log = "\n".join(log_parts)
                    run.completed_at = datetime.utcnow()
                    run.duration_ms = duration_ms
                    await session.commit()

                # Update SOP last_run_at
                result = await session.execute(
                    select(SOP).where(SOP.sop_id == sop_id)
                )
                sop = result.scalar_one_or_none()
                if sop:
                    sop.last_run_at = datetime.utcnow()
                    await session.commit()

            logger.info(
                "automation_run_completed",
                run_id=run_id, sop_id=sop_id,
                status=final_status, steps=f"{completed}/{total_steps}",
                duration_ms=duration_ms,
            )

        except Exception as exc:
            logger.error("automation_run_failed", run_id=run_id, error=str(exc))
            try:
                async with async_session_maker() as session:
                    result = await session.execute(
                        select(AutomationRun).where(AutomationRun.run_id == run_id)
                    )
                    run = result.scalar_one_or_none()
                    if run:
                        run.status = "failed"
                        run.error = str(exc)
                        run.output_log = "\n".join(log_parts)
                        run.completed_at = datetime.utcnow()
                        run.duration_ms = int((time.time() - t0) * 1000)
                        await session.commit()
            except Exception:
                pass

    async def _run_step(self, tool: str, command: str, product_id: str, step: dict | None = None) -> str:
        """Execute a single automation step using the appropriate tool.

        For tools that take a single argument (e.g. terminal, read_file), `command` is passed as-is.
        For write_file, the step must provide content via step["text"] or step["content"]; command is the path.
        """
        from app.tools import get_tool_by_name

        step = step or {}
        tool_func = get_tool_by_name(tool)
        if tool_func is None:
            # Fallback: use terminal for unknown tools
            tool_func = get_tool_by_name("terminal")

        if tool_func is None:
            return f"Tool '{tool}' not available"

        # write_file(path, text) requires two arguments; scheduler step has command (path) and optional text/content
        if tool == "write_file":
            path = command
            text = step.get("text") or step.get("content") or ""
            args = (path, text)
        else:
            args = (command,)

        try:
            if asyncio.iscoroutinefunction(tool_func):
                result = await tool_func(*args)
            else:
                result = await asyncio.get_event_loop().run_in_executor(None, lambda: tool_func(*args))
            return str(result) if result else "(no output)"
        except Exception as exc:
            raise RuntimeError(f"Step failed ({tool}): {exc}")

    async def run_now(self, sop_id: str, product_id: str) -> str:
        """Trigger an immediate manual run. Returns the run_id."""
        from app.db.database import async_session_maker
        from app.models.automation_run import AutomationRun
        from app.models.sop import SOP
        from sqlalchemy import select

        # Verify SOP exists
        async with async_session_maker() as session:
            result = await session.execute(select(SOP).where(SOP.sop_id == sop_id))
            sop = result.scalar_one_or_none()
            if not sop:
                raise ValueError("Automation not found")

        # Run in background
        asyncio.create_task(self._execute_automation(sop_id, product_id))
        return sop_id


# Global instance
automation_scheduler = AutomationScheduler()
