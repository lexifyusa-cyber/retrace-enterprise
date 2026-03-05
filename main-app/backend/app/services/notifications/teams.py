"""
Microsoft Teams notification sender via Incoming Webhook (Adaptive Cards).
"""

from typing import Optional

import httpx
import structlog

logger = structlog.get_logger()


async def send_teams_message(
    webhook_url: str,
    text: str,
    *,
    title: Optional[str] = None,
    fields: Optional[list[dict]] = None,
) -> bool:
    """Post an Adaptive Card to a Teams channel via Incoming Webhook.

    Returns True on success, False on failure (never raises).
    """
    body_items: list[dict] = []
    if title:
        body_items.append(
            {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium"}
        )
    body_items.append({"type": "TextBlock", "text": text, "wrap": True})
    if fields:
        facts = [{"title": f["label"], "value": f["value"]} for f in fields]
        body_items.append({"type": "FactSet", "facts": facts})

    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body_items,
                },
            }
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=card)
            resp.raise_for_status()
            logger.info("teams_notification_sent")
            return True
    except Exception as exc:
        logger.error("teams_notification_failed", error=str(exc))
        return False
