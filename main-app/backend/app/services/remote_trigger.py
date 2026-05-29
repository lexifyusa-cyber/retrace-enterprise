"""
Remote Trigger Service — fires the agent on external webhook POSTs.

Sister to FollowupScheduler / UrlMonitor. No periodic timer — these are
event-driven. Each registered trigger has a long random token; an
external system POSTing to `/api/v1/triggers/<token>` causes retrace to
inject a synthetic user message into the trigger's parent conversation,
combining the stored prompt template and the incoming payload.

Why no APScheduler here: the trigger has no schedule. It sleeps until an
HTTP request arrives and then runs the same `_inject_prompt` path
everything else in this family uses.
"""

import json
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

import structlog

logger = structlog.get_logger()


# Max payload bytes we accept. Most webhooks (GitHub, Slack, Linear) are
# well under 100KB; cap higher to be safe but not so high that a runaway
# webhook fills the agent context budget.
_MAX_PAYLOAD_BYTES = 256_000

# How much of the rendered payload we include in the prompt to the agent.
# Beyond this we truncate with a note — the agent can use the trigger_id
# to fetch the full payload via a follow-up tool if we ever add one.
_PAYLOAD_PREVIEW_CHARS = 4000


class RemoteTriggerService:
    """Create / list / cancel triggers + fire them on webhook hits."""

    async def create(
        self,
        user_id: str,
        conversation_id: str,
        prompt_template: str,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new trigger. Returns the row dict WITH the token (only
        time the token is exposed — list_for_user redacts it).

        LLMs that have used Jinja-style templating elsewhere occasionally
        write '{{payload}}' or '{payload}' placeholders in the template
        thinking we'll substitute them. We do NOT substitute (the actual
        payload arrives as a structured code block appended after the
        template, so the agent always has it). Strip these literals so
        they don't leak as visible cruft in the fired turn.
        """
        from app.db.database import async_session_maker
        from app.models.remote_trigger import RemoteTrigger

        import re as _re
        cleaned = _re.sub(r"\s*\{\{?\s*payload\s*\}?\}\s*", " ", prompt_template).strip()
        if cleaned != prompt_template.strip():
            logger.info(
                "remote_trigger_template_payload_placeholder_stripped",
                before_len=len(prompt_template), after_len=len(cleaned),
            )
        prompt_template = cleaned

        token = secrets.token_urlsafe(32)  # ≈ 43 chars
        async with async_session_maker() as session:
            row = RemoteTrigger(
                trigger_id=str(uuid4()),
                user_id=user_id,
                conversation_id=conversation_id,
                token=token,
                prompt_template=prompt_template,
                description=description,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            payload = row.to_dict(include_token=True)
        logger.info(
            "remote_trigger_created",
            trigger_id=payload["trigger_id"], conv_id=conversation_id,
            description=(description or "")[:80],
        )
        return payload

    async def cancel(self, trigger_id: str, user_id: str) -> bool:
        from app.db.database import async_session_maker
        from app.models.remote_trigger import RemoteTrigger
        from sqlalchemy import select

        async with async_session_maker() as session:
            result = await session.execute(
                select(RemoteTrigger).where(
                    RemoteTrigger.trigger_id == trigger_id,
                    RemoteTrigger.user_id == user_id,
                )
            )
            row = result.scalar_one_or_none()
            if not row or not row.is_active:
                return False
            row.is_active = False
            await session.commit()
        logger.info("remote_trigger_cancelled", trigger_id=trigger_id)
        return True

    async def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        from app.db.database import async_session_maker
        from app.models.remote_trigger import RemoteTrigger
        from sqlalchemy import select

        async with async_session_maker() as session:
            result = await session.execute(
                select(RemoteTrigger)
                .where(RemoteTrigger.user_id == user_id, RemoteTrigger.is_active == True)
                .order_by(RemoteTrigger.created_at.desc())
            )
            # Token is redacted — once a trigger URL is in the wild, the
            # agent shouldn't make it easy to leak the full URL back into
            # chat history.
            return [r.to_dict(include_token=False) for r in result.scalars().all()]

    async def fire(self, token: str, payload_text: str) -> Dict[str, Any]:
        """Called by the public webhook endpoint. Looks up the trigger, builds
        a prompt, and delivers via _inject_prompt. Returns metadata so the
        webhook caller (e.g. GitHub Actions) sees what happened.

        `payload_text` is the raw request body as text. The caller (api)
        already enforced the size cap.
        """
        from app.db.database import async_session_maker
        from app.models.remote_trigger import RemoteTrigger
        from sqlalchemy import select

        async with async_session_maker() as session:
            result = await session.execute(
                select(RemoteTrigger).where(
                    RemoteTrigger.token == token, RemoteTrigger.is_active == True
                )
            )
            row = result.scalar_one_or_none()
            if not row:
                return {"accepted": False, "reason": "unknown or inactive token"}
            row.fire_count += 1
            row.last_fired_at = datetime.utcnow()
            row.last_error = None
            await session.commit()
            trigger_id = row.trigger_id
            user_id = row.user_id
            conversation_id = row.conversation_id
            prompt_template = row.prompt_template
            description = row.description or "remote trigger"

        # Pretty-print JSON when possible so the agent sees structured data
        # rather than a one-line blob. Fall back to raw text for non-JSON
        # bodies (e.g. plaintext webhooks).
        try:
            parsed = json.loads(payload_text or "")
            payload_pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        except Exception:
            payload_pretty = payload_text or ""

        if len(payload_pretty) > _PAYLOAD_PREVIEW_CHARS:
            payload_pretty = (
                payload_pretty[:_PAYLOAD_PREVIEW_CHARS]
                + f"\n... [truncated, {len(payload_pretty) - _PAYLOAD_PREVIEW_CHARS} more chars]"
            )

        prompt = (
            f"[remote-trigger:{trigger_id}] '{description}' fired.\n\n"
            f"{prompt_template}\n\n"
            f"Payload:\n```json\n{payload_pretty}\n```"
        )

        logger.info(
            "remote_trigger_fire",
            trigger_id=trigger_id, user_id=user_id, conv_id=conversation_id,
            payload_bytes=len(payload_text or ""),
        )

        try:
            from app.services.followup_scheduler import followup_scheduler
            await followup_scheduler._inject_prompt(user_id, conversation_id, prompt)
        except Exception as exc:
            err = str(exc)[:500]
            logger.warning("remote_trigger_fire_failed", trigger_id=trigger_id, error=err)
            # Persist the error so the user can debug via list.
            try:
                async with async_session_maker() as session:
                    result = await session.execute(
                        select(RemoteTrigger).where(RemoteTrigger.trigger_id == trigger_id)
                    )
                    row = result.scalar_one_or_none()
                    if row:
                        row.last_error = err
                        await session.commit()
            except Exception:
                pass
            return {"accepted": True, "delivered": False, "trigger_id": trigger_id, "error": err}

        return {"accepted": True, "delivered": True, "trigger_id": trigger_id}


# Module-global singleton.
remote_trigger_service = RemoteTriggerService()
