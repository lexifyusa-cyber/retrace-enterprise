"""Scheduled task to auto-disable users who haven't logged in for N days."""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select
import structlog

from app.db.database import async_session_maker
from app.models.tenant import Tenant
from app.models.user import User
from app.models.audit_log import AuditLog

logger = structlog.get_logger()


async def check_inactive_users():
    """Check all tenants for inactive users and disable them per policy."""
    try:
        async with async_session_maker() as session:
            result = await session.execute(select(Tenant))
            tenants = result.scalars().all()

            for tenant in tenants:
                policy = tenant.password_policy or {}
                inactive_days = policy.get("auto_disable_after_days", 0)
                if not inactive_days or inactive_days <= 0:
                    continue

                cutoff = datetime.utcnow() - timedelta(days=inactive_days)
                user_result = await session.execute(
                    select(User).where(
                        User.tenant_id == tenant.tenant_id,
                        User.is_active == True,
                        User.role != "zero_admin",
                        User.status == "active",
                        (User.last_login_at < cutoff) | (User.last_login_at == None),
                    )
                )
                inactive_users = user_result.scalars().all()

                for user in inactive_users:
                    if user.created_at and user.created_at > cutoff:
                        continue
                    user.is_active = False
                    user.status = "disabled"
                    log = AuditLog(
                        action="auto_disabled_inactive",
                        actor_user_id=None,
                        actor_email="system",
                        tenant_id=tenant.tenant_id,
                        target_type="user",
                        target_id=user.user_id,
                        details={"inactive_days": inactive_days, "last_login": user.last_login_at.isoformat() if user.last_login_at else None},
                    )
                    session.add(log)
                    logger.info("auto_disabled_inactive_user", user_id=user.user_id, email=user.email, inactive_days=inactive_days)

            await session.commit()
    except Exception as exc:
        logger.error("inactive_user_check_failed", error=str(exc))


class InactiveUserChecker:
    def __init__(self):
        self._task = None

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            await asyncio.sleep(86400)  # Check once per day
            await check_inactive_users()

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


inactive_user_checker = InactiveUserChecker()
