"""
Slack notification sender via Incoming Webhook.

No SDK — just a single POST with a JSON payload.
"""

from typing import Optional

import httpx
import structlog

logger = structlog.get_logger()


async def send_slack_message(
    webhook_url: str,
    text: str,
    *,
    title: Optional[str] = None,
    fields: Optional[list[dict]] = None,
    color: str = "#6366f1",
) -> bool:
    """Post a rich message to a Slack channel via Incoming Webhook.

    Returns True on success, False on failure (never raises).
    """
    blocks = []
    if title:
        blocks.append(
            {"type": "header", "text": {"type": "plain_text", "text": title}}
        )
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    )
    if fields:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{f['label']}*\n{f['value']}"}
                    for f in fields
                ],
            }
        )

    payload = {"text": title or text, "blocks": blocks}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
            logger.info("slack_notification_sent")
            return True
    except Exception as exc:
        logger.error("slack_notification_failed", error=str(exc))
        return False
