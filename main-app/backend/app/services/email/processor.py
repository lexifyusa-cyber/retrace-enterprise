"""
Email processing pipeline — the core orchestrator.

Resolves inbound email → team, runs the query through the existing
QueryService, then fires all enabled action modes in parallel.
"""

import asyncio
import json
import re
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_inbox import EmailInbox
from app.models.email_message import EmailMessage
from app.models.email_connection import EmailConnection
from app.services.email.base import InboundEmail
from app.services.email.factory import get_provider
from app.services.notifications.notifier import notifier, NotificationPayload
from app.services.query_service import query_service

logger = structlog.get_logger()

# ── Helpers ───────────────────────────────────────────────────────────

_QUOTED_REPLY_RE = re.compile(
    r"(^On .+?wrote:$|^-{2,}\s*Original Message|^>{1,}\s)", re.MULTILINE
)


def _strip_quoted_replies(text: str) -> str:
    """Remove quoted reply blocks from plain-text email body."""
    match = _QUOTED_REPLY_RE.search(text)
    if match:
        return text[: match.start()].strip()
    return text.strip()


def _build_question(subject: str, body: str) -> str:
    """Combine subject and cleaned body into a single question string."""
    body = _strip_quoted_replies(body)
    if subject and body:
        return f"Subject: {subject}\n\n{body}"
    return body or subject or ""


def _html_to_plain(html: str) -> str:
    """Very basic HTML tag stripping (no external dependency)."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


# ── Classification prompt ─────────────────────────────────────────────

CLASSIFY_PROMPT = """Classify this email into EXACTLY one category.

Categories: bug_report, feature_request, general_question, billing, security, documentation, other

Email subject: {subject}
Email body:
{body}

Respond with ONLY a JSON object: {{"category": "<category>", "confidence": <0.0-1.0>, "summary": "<one-line summary>"}}"""


# ── Pipeline ──────────────────────────────────────────────────────────


class EmailProcessingPipeline:
    """Process a single inbound email through all enabled action modes."""

    async def process(
        self,
        email: InboundEmail,
        session: AsyncSession,
        inbox_override: Optional["EmailInbox"] = None,
    ) -> Optional[EmailMessage]:
        """Resolve, query, execute actions, and persist the message record."""

        # 1. Resolve recipient → inbox → team
        inbox = inbox_override or await self._resolve_inbox(email.to_address, session)
        if not inbox:
            logger.warning("email_no_inbox_match", to=email.to_address)
            return None

        if not inbox.is_active:
            logger.info("email_inbox_inactive", inbox_id=inbox.inbox_id)
            return None

        config: dict = inbox.action_config or {}
        team_id = inbox.team_id

        # 2. Parse body
        body_text = email.body_text
        if not body_text and email.body_html:
            body_text = _html_to_plain(email.body_html)
        question = _build_question(email.subject, body_text)

        if not question.strip():
            logger.info("email_empty_body", inbox_id=inbox.inbox_id)
            return None

        # 3. Query the team knowledge base
        try:
            result = await query_service.process_query(
                question=question,
                team_ids=[team_id],
                session=session,
            )
        except Exception as exc:
            logger.error("email_query_failed", error=str(exc))
            result = {
                "answer": "I was unable to process your question at this time.",
                "confidence_score": 0.0,
                "sources": [],
                "related_queries": [],
            }

        ai_answer: str = result["answer"]
        confidence: float = result["confidence_score"]

        # Persist the message record
        msg = EmailMessage(
            inbox_id=inbox.inbox_id,
            team_id=team_id,
            from_address=email.from_address,
            subject=email.subject,
            body_text=body_text,
            body_html=email.body_html,
            provider_message_id=email.provider_message_id,
            ai_response=ai_answer,
            confidence_score=confidence,
            status="processing",
            actions_taken=[],
        )
        session.add(msg)
        await session.flush()

        # 4. Fire enabled action modes concurrently
        actions_taken: list[dict] = []
        tasks = []

        if config.get("auto_reply"):
            tasks.append(
                self._action_auto_reply(
                    msg, inbox, config, ai_answer, confidence, session
                )
            )

        if config.get("draft_and_review"):
            tasks.append(self._action_draft(msg, actions_taken))

        if config.get("classify_and_route"):
            tasks.append(
                self._action_classify_route(
                    msg, inbox, config, email, session
                )
            )

        if config.get("summarize_and_notify"):
            tasks.append(
                self._action_summarize_notify(msg, inbox, config, email)
            )

        action_results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in action_results:
            if isinstance(r, dict):
                actions_taken.append(r)
            elif isinstance(r, Exception):
                actions_taken.append({"action": "error", "detail": str(r)})

        msg.actions_taken = actions_taken

        # Determine final status
        statuses = [a.get("action") for a in actions_taken if isinstance(a, dict)]
        if "auto_replied" in statuses:
            msg.status = "auto_replied"
        elif "drafted" in statuses:
            msg.status = "drafted"
        elif "classified" in statuses:
            msg.status = "classified"
        elif "escalated" in statuses:
            msg.status = "escalated"
        else:
            msg.status = "processed"

        await session.flush()
        return msg

    # ── Resolve ───────────────────────────────────────────────────────

    async def _resolve_inbox(
        self, to_address: str, session: AsyncSession
    ) -> Optional[EmailInbox]:
        to_lower = to_address.replace("&lt;", "<").replace("&gt;", ">").lower().strip()
        m = re.search(r"<([^>]+)>", to_lower)
        if m:
            to_lower = m.group(1).strip()
        result = await session.execute(
            select(EmailInbox).where(EmailInbox.email_address == to_lower)
        )
        return result.scalar_one_or_none()

    # ── Action: Auto-Reply ────────────────────────────────────────────

    async def _action_auto_reply(
        self,
        msg: EmailMessage,
        inbox: EmailInbox,
        config: dict,
        ai_answer: str,
        confidence: float,
        session: AsyncSession,
    ) -> dict:
        threshold = config.get("auto_reply_confidence_threshold", 0.9)

        if confidence >= threshold:
            # Send the reply via the email provider
            try:
                connection = await session.get(EmailConnection, inbox.connection_id)
                if connection:
                    token_data = json.loads(connection.oauth_token_encrypted or "{}")
                    provider = get_provider(connection)
                    reply_html = (
                        f"<div>{ai_answer}</div>"
                        f"<br><hr><small>This response was generated by AI. "
                        f"If this doesn't help, reply and a human will follow up.</small>"
                    )
                    await provider.send_reply(
                        access_token=token_data.get("access_token", ""),
                        to=msg.from_address,
                        subject=f"Re: {msg.subject}",
                        body_html=reply_html,
                        in_reply_to=msg.provider_message_id,
                    )
                    return {"action": "auto_replied", "confidence": confidence}
            except Exception as exc:
                logger.error("auto_reply_send_failed", error=str(exc))
                return {"action": "auto_reply_failed", "error": str(exc)}

        # Below threshold → escalate
        return await self._escalate(msg, inbox, config, ai_answer, confidence)

    async def _escalate(
        self,
        msg: EmailMessage,
        inbox: EmailInbox,
        config: dict,
        ai_answer: str,
        confidence: float,
    ) -> dict:
        channel = config.get("escalation_channel", "slack")
        payload = NotificationPayload(
            title=f"AI Escalation: {msg.subject}",
            body=(
                f"*From:* {msg.from_address}\n"
                f"*Confidence:* {confidence:.0%}\n\n"
                f"*Question:*\n{msg.body_text[:500]}\n\n"
                f"*AI Draft Answer:*\n{ai_answer[:1000]}"
            ),
            fields=[
                {"label": "Team", "value": msg.team_id},
                {"label": "Confidence", "value": f"{confidence:.0%}"},
            ],
        )
        sent = await notifier.send(channel, config, payload)
        return {
            "action": "escalated",
            "channel": channel,
            "sent": sent,
            "confidence": confidence,
        }

    # ── Action: Draft & Review ────────────────────────────────────────

    async def _action_draft(
        self, msg: EmailMessage, actions_taken: list[dict]
    ) -> dict:
        # The message is already persisted with ai_response; mark as drafted
        # so the frontend can show it in the review queue.
        return {"action": "drafted"}

    # ── Action: Classify & Route ──────────────────────────────────────

    async def _action_classify_route(
        self,
        msg: EmailMessage,
        inbox: EmailInbox,
        config: dict,
        email: InboundEmail,
        session: AsyncSession,
    ) -> dict:
        from app.api.settings import get_active_llm_settings

        try:
            llm_settings = await get_active_llm_settings(session)
            from openai import AsyncOpenAI

            api_key = llm_settings.get("api_key", "")
            base_url = llm_settings.get("api_url") or None
            model = llm_settings.get("model_name", "gpt-4o")

            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            prompt = CLASSIFY_PROMPT.format(
                subject=msg.subject, body=msg.body_text[:2000]
            )
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=200,
            )
            raw = resp.choices[0].message.content or "{}"
            classification = json.loads(raw)
        except Exception as exc:
            logger.error("email_classify_failed", error=str(exc))
            classification = {"category": "other", "confidence": 0, "summary": str(exc)}

        msg.classification = classification

        # Route to the matching channel
        category = classification.get("category", "other")
        route_rules: list[dict] = config.get("route_rules", [])
        matched_rule = next(
            (r for r in route_rules if r.get("category") == category), None
        )

        routed = False
        if matched_rule:
            target_channel = matched_rule.get("target", "slack")
            payload = NotificationPayload(
                title=f"[{category}] {msg.subject}",
                body=(
                    f"*From:* {msg.from_address}\n"
                    f"*Category:* {category}\n"
                    f"*Summary:* {classification.get('summary', '')}\n\n"
                    f"{msg.body_text[:500]}"
                ),
                fields=[
                    {"label": "Category", "value": category},
                    {"label": "Team", "value": msg.team_id},
                ],
            )
            routed = await notifier.send(target_channel, config, payload)

        return {
            "action": "classified",
            "classification": classification,
            "routed": routed,
            "rule": matched_rule,
        }

    # ── Action: Summarize & Notify ────────────────────────────────────

    async def _action_summarize_notify(
        self,
        msg: EmailMessage,
        inbox: EmailInbox,
        config: dict,
        email: InboundEmail,
    ) -> dict:
        channel = config.get("escalation_channel", "slack")
        suggested = msg.ai_response or ""
        payload = NotificationPayload(
            title=f"New AI Email: {msg.subject}",
            body=(
                f"*From:* {msg.from_address}\n\n"
                f"*Summary:*\n{msg.body_text[:300]}\n\n"
                f"*Suggested Response:*\n{suggested[:500]}"
            ),
            fields=[
                {"label": "Team", "value": msg.team_id},
                {"label": "Confidence", "value": f"{msg.confidence_score:.0%}" if msg.confidence_score else "N/A"},
            ],
        )
        sent = await notifier.send(channel, config, payload)
        return {"action": "summarized_notified", "channel": channel, "sent": sent}


email_processor = EmailProcessingPipeline()
