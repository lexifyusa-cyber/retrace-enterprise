"""
Unified notification dispatcher.

Picks the right channel (email / slack / teams) and sends the payload.
"""

from dataclasses import dataclass, field
from typing import Optional

import structlog

from app.services.notifications.slack import send_slack_message
from app.services.notifications.teams import send_teams_message

logger = structlog.get_logger()


@dataclass
class NotificationPayload:
    """Channel-agnostic notification content."""

    title: str
    body: str
    fields: list[dict] = field(default_factory=list)
    reply_to_email: Optional[str] = None
    email_subject: Optional[str] = None


class Notifier:
    """Send a notification to the appropriate channel."""

    async def send(
        self,
        channel: str,
        config: dict,
        payload: NotificationPayload,
        *,
        email_provider=None,
        access_token: Optional[str] = None,
    ) -> bool:
        """Dispatch *payload* to *channel*.

        *config* must contain the relevant webhook URL or provider info.
        For email, pass the provider adapter and token so we can call send_reply.
        """
        match channel:
            case "slack":
                url = config.get("slack_webhook_url", "")
                if not url:
                    logger.warning("notifier_no_slack_url")
                    return False
                return await send_slack_message(
                    url,
                    payload.body,
                    title=payload.title,
                    fields=payload.fields,
                )
            case "teams":
                url = config.get("teams_webhook_url", "")
                if not url:
                    logger.warning("notifier_no_teams_url")
                    return False
                return await send_teams_message(
                    url,
                    payload.body,
                    title=payload.title,
                    fields=payload.fields,
                )
            case "email":
                if not email_provider or not access_token:
                    logger.warning("notifier_no_email_provider")
                    return False
                try:
                    await email_provider.send_reply(
                        access_token=access_token,
                        to=payload.reply_to_email or "",
                        subject=payload.email_subject or payload.title,
                        body_html=payload.body,
                    )
                    return True
                except Exception as exc:
                    logger.error("notifier_email_failed", error=str(exc))
                    return False
            case _:
                logger.warning("notifier_unknown_channel", channel=channel)
                return False


notifier = Notifier()
