"""
Email receiver API — connections, inboxes, inbound webhook, drafts, message log.
"""

import json
import re
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks, status

# Extract email from "Display Name" <email@domain.com> for matching
_EMAIL_IN_ADDR_RE = re.compile(r"<([^>]+)>")
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.db.database import get_session
from app.models.email_connection import EmailConnection
from app.models.email_inbox import EmailInbox
from app.models.email_message import EmailMessage
from app.services.email.base import InboundEmail
from app.services.email.factory import get_provider
from app.services.email.processor import email_processor
from app.core.security import get_optional_user, require_role, CurrentUser

logger = structlog.get_logger()
router = APIRouter()
product_email_router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────


class ConnectionCreate(BaseModel):
    provider: str = Field(..., pattern="^(microsoft|google|zoho)$")
    tenant_name: str = Field(..., min_length=1, max_length=255)
    oauth_code: str = Field(..., min_length=1)
    redirect_uri: str = Field(default="")
    client_id: str = Field(default="")
    client_secret: str = Field(default="")
    tenant_id: str = Field(default="common")
    slack_webhook_url: Optional[str] = None
    teams_webhook_url: Optional[str] = None


class ConnectionUpdate(BaseModel):
    tenant_name: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    teams_webhook_url: Optional[str] = None


class ReconnectData(BaseModel):
    oauth_code: str = Field(..., min_length=1)
    redirect_uri: str = Field(default="")


class ConnectionResponse(BaseModel):
    connection_id: str
    provider: str
    tenant_name: str
    status: str
    slack_webhook_url: Optional[str] = None
    teams_webhook_url: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class InboxCreate(BaseModel):
    connection_id: str
    display_name: str = Field(default="AI Inbox")
    action_config: Optional[dict] = None


class InboxUpdate(BaseModel):
    is_active: Optional[bool] = None
    action_config: Optional[dict] = None


class InboxResponse(BaseModel):
    inbox_id: str
    product_id: str
    connection_id: str
    email_address: str
    is_active: bool
    action_config: dict
    created_at: Optional[str] = None


class DraftAction(BaseModel):
    edited_response: Optional[str] = None


class MessageResponse(BaseModel):
    message_id: str
    inbox_id: str
    product_id: str
    from_address: str
    subject: str
    body_text: str
    received_at: Optional[str] = None
    status: str
    ai_response: Optional[str] = None
    confidence_score: Optional[float] = None
    classification: Optional[dict] = None
    actions_taken: Optional[list] = None


class UnreadMessageResponse(BaseModel):
    """One unread message from the provider (read-only view)."""
    from_address: str
    subject: str
    received_at: Optional[str] = None
    body_text: str = ""


# ── Email Connections ─────────────────────────────────────────────────


@router.get("/connections", response_model=list[ConnectionResponse])
async def list_connections(
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(require_role("admin")),
):
    query = select(EmailConnection)
    if current_user.tenant_id:
        query = query.where(EmailConnection.tenant_id == current_user.tenant_id)
    result = await session.execute(query)
    return [
        ConnectionResponse(**c.to_dict()) for c in result.scalars().all()
    ]


@router.post("/connections", response_model=ConnectionResponse, status_code=201)
async def create_connection(
    data: ConnectionCreate,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(require_role("admin")),
):
    conn = EmailConnection(
        tenant_id=current_user.tenant_id,
        provider=data.provider,
        tenant_name=data.tenant_name,
        client_id=data.client_id or None,
        client_secret=data.client_secret or None,
        slack_webhook_url=data.slack_webhook_url,
        teams_webhook_url=data.teams_webhook_url,
        status="active",
    )

    # Exchange OAuth code for tokens
    try:
        provider = get_provider(
            conn,
            client_id=data.client_id,
            client_secret=data.client_secret,
            tenant_id=data.tenant_id,
        )
        token_data = await provider.connect(data.oauth_code, data.redirect_uri)
        conn.oauth_token_encrypted = json.dumps(token_data)
    except Exception as exc:
        logger.error("email_oauth_failed", error=str(exc))
        raise HTTPException(
            status_code=400,
            detail=f"OAuth token exchange failed: {exc}",
        )

    session.add(conn)
    await session.flush()
    return ConnectionResponse(**conn.to_dict())


@router.patch("/connections/{connection_id}", response_model=ConnectionResponse)
async def update_connection(
    connection_id: str,
    data: ConnectionUpdate,
    session: AsyncSession = Depends(get_session),
):
    conn = await session.get(EmailConnection, connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    if data.tenant_name is not None:
        conn.tenant_name = data.tenant_name
    if data.slack_webhook_url is not None:
        conn.slack_webhook_url = data.slack_webhook_url
    if data.teams_webhook_url is not None:
        conn.teams_webhook_url = data.teams_webhook_url
    await session.flush()
    return ConnectionResponse(**conn.to_dict())


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str, session: AsyncSession = Depends(get_session)
):
    conn = await session.get(EmailConnection, connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    await session.delete(conn)
    await session.flush()


@router.post("/connections/{connection_id}/reconnect")
async def reconnect_connection(
    connection_id: str,
    data: ReconnectData,
    session: AsyncSession = Depends(get_session),
):
    """Exchange a new OAuth code to refresh tokens on an existing connection."""
    conn = await session.get(EmailConnection, connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    client_id = getattr(conn, "client_id", None) or ""
    client_secret = getattr(conn, "client_secret", None) or ""
    try:
        provider = get_provider(
            conn,
            client_id=client_id,
            client_secret=client_secret,
            tenant_id="common",
        )
        token_data = await provider.connect(data.oauth_code, data.redirect_uri)
        conn.oauth_token_encrypted = json.dumps(token_data)
        conn.status = "active"
        await session.flush()
        has_refresh = bool(token_data.get("refresh_token"))
        return {
            "success": True,
            "message": f"Reconnected. Refresh token: {'yes' if has_refresh else 'NO (use access_type=offline)'}",
            "user_email": token_data.get("user_email", ""),
        }
    except Exception as exc:
        logger.error("reconnect_failed", error=str(exc))
        raise HTTPException(400, f"Reconnect failed: {exc}")


@router.post("/connections/{connection_id}/test")
async def test_connection(
    connection_id: str, session: AsyncSession = Depends(get_session)
):
    conn = await session.get(EmailConnection, connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    try:
        token_data = json.loads(conn.oauth_token_encrypted or "{}")
        provider = get_provider(conn)
        from datetime import datetime, timedelta

        msgs = await provider.fetch_new_messages(
            access_token=token_data.get("access_token", ""),
            since=datetime.utcnow() - timedelta(minutes=5),
        )
        return {
            "success": True,
            "message": f"Connected. Found {len(msgs)} recent messages.",
        }
    except Exception as exc:
        return {"success": False, "message": str(exc)}


@router.post("/connections/{connection_id}/sync")
async def sync_connection(
    connection_id: str, session: AsyncSession = Depends(get_session)
):
    """Fetch new messages from the provider and process them into drafts/messages.

    Only messages sent to an AI inbox (EmailInbox) linked to this connection
    are processed. Duplicates by provider_message_id are skipped.
    """
    conn = await session.get(EmailConnection, connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")

    # Inboxes linked to this connection (each has an email_address we accept)
    result = await session.execute(
        select(EmailInbox).where(
            EmailInbox.connection_id == connection_id,
            EmailInbox.is_active == True,
        )
    )
    inbox_list = result.scalars().all()
    if not inbox_list:
        return {
            "synced": 0,
            "message": "No AI inboxes linked to this connection. Add an AI Email to a product first (Products → product → AI Email).",
        }

    inbox_emails = {i.email_address.lower().strip() for i in inbox_list}
    inbox_ids = [i.inbox_id for i in inbox_list]

    token_data = json.loads(conn.oauth_token_encrypted or "{}")
    client_id = getattr(conn, "client_id", None) or ""
    client_secret = getattr(conn, "client_secret", None) or ""
    provider = get_provider(
        conn,
        client_id=client_id,
        client_secret=client_secret,
        tenant_id="common",
    )
    access_token = token_data.get("access_token", "")
    since = datetime.utcnow() - timedelta(minutes=30)

    async def _fetch():
        return await provider.fetch_new_messages(
            access_token=access_token,
            since=since,
        )

    try:
        messages = await _fetch()
    except Exception as exc:
        err_str = str(exc)
        is_401 = "401" in err_str
        refresh_token = token_data.get("refresh_token", "")
        if is_401 and refresh_token and client_id and client_secret and conn.provider == "zoho":
            try:
                from app.services.email.zoho import ZohoProvider
                zoho = ZohoProvider(client_id=client_id, client_secret=client_secret)
                new_tokens = await zoho.refresh_token(refresh_token)
                token_data["access_token"] = new_tokens["access_token"]
                token_data["expires_in"] = new_tokens.get("expires_in", 3600)
                if new_tokens.get("refresh_token"):
                    token_data["refresh_token"] = new_tokens["refresh_token"]
                conn.oauth_token_encrypted = json.dumps(token_data)
                await session.flush()
                access_token = token_data["access_token"]
                messages = await _fetch()
            except Exception as refresh_exc:
                logger.error("email_token_refresh_failed", error=str(refresh_exc))
                raise HTTPException(
                    status_code=401,
                    detail="Token expired. Reconnect the email provider in Settings (remove and add again with a new authorization code).",
                )
        else:
            logger.error("email_sync_fetch_failed", error=err_str)
            raise HTTPException(
                status_code=400,
                detail=f"Failed to fetch messages from provider: {exc}",
            )

    def _normalize_to_email(addr: str) -> str:
        if not addr:
            return ""
        addr = addr.replace("&lt;", "<").replace("&gt;", ">").strip().lower()
        m = _EMAIL_IN_ADDR_RE.search(addr)
        return m.group(1).strip().lower() if m else addr

    logger.info("email_sync_fetched", connection_id=connection_id, fetched=len(messages), inbox_emails=list(inbox_emails))

    # Build a quick lookup: normalized email -> inbox
    inbox_by_email = {i.email_address.lower().strip(): i for i in inbox_list}
    default_inbox = inbox_list[0] if inbox_list else None

    processed = 0
    for email in messages:
        to_addr = _normalize_to_email(email.to_address or "")
        # If to_addr is non-empty and doesn't match any inbox, skip
        if to_addr and to_addr not in inbox_emails:
            continue
        # Determine which inbox this message belongs to
        matched_inbox = inbox_by_email.get(to_addr, default_inbox) if to_addr else default_inbox
        if not matched_inbox:
            continue
        # Skip if we already processed this message
        existing = await session.execute(
            select(EmailMessage).where(
                EmailMessage.provider_message_id == email.provider_message_id,
                EmailMessage.inbox_id.in_(inbox_ids),
            )
        )
        if existing.scalars().first() is not None:
            continue
        try:
            msg = await email_processor.process(email, session, inbox_override=matched_inbox)
            if msg:
                processed += 1
        except Exception as exc:
            logger.warning("email_sync_process_failed", error=str(exc), subject=email.subject)

    await session.commit()
    return {
        "synced": processed,
        "message": f"Synced {processed} new message(s). Check Email Drafts for the product(s) linked to this inbox.",
    }


# ── Email Inboxes (per product) ──────────────────────────────────────


@product_email_router.get("/{product_id}/inbox", response_model=Optional[InboxResponse])
async def get_inbox(
    product_id: str, session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(EmailInbox).where(EmailInbox.product_id == product_id)
    )
    inbox = result.scalar_one_or_none()
    if not inbox:
        return None
    return InboxResponse(**inbox.to_dict())


@product_email_router.post("/{product_id}/inbox", response_model=InboxResponse, status_code=201)
async def create_inbox(
    product_id: str,
    data: InboxCreate,
    session: AsyncSession = Depends(get_session),
):
    # Check if inbox already exists
    existing = await session.execute(
        select(EmailInbox).where(EmailInbox.product_id == product_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Product already has an AI inbox")

    conn = await session.get(EmailConnection, data.connection_id)
    if not conn:
        raise HTTPException(400, "Email connection not found")

    # Generate the inbox address via the provider
    try:
        token_data = json.loads(conn.oauth_token_encrypted or "{}")
        provider = get_provider(conn)
        email_address = await provider.create_inbox(
            display_name=data.display_name,
            access_token=token_data.get("access_token", ""),
        )
    except Exception as exc:
        logger.error("create_inbox_failed", error=str(exc))
        raise HTTPException(400, f"Failed to create inbox: {exc}")

    inbox = EmailInbox(
        product_id=product_id,
        connection_id=data.connection_id,
        email_address=email_address.lower(),
        is_active=True,
        action_config=data.action_config
        or {
            "auto_reply": False,
            "auto_reply_confidence_threshold": 0.9,
            "draft_and_review": True,
            "classify_and_route": False,
            "summarize_and_notify": False,
            "escalation_channel": "slack",
            "slack_webhook_url": conn.slack_webhook_url,
            "teams_webhook_url": conn.teams_webhook_url,
            "route_rules": [],
        },
    )
    session.add(inbox)
    await session.flush()
    return InboxResponse(**inbox.to_dict())


@product_email_router.patch("/{product_id}/inbox", response_model=InboxResponse)
async def update_inbox(
    product_id: str,
    data: InboxUpdate,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(EmailInbox).where(EmailInbox.product_id == product_id)
    )
    inbox = result.scalar_one_or_none()
    if not inbox:
        raise HTTPException(404, "No inbox for this product")
    if data.is_active is not None:
        inbox.is_active = data.is_active
    if data.action_config is not None:
        inbox.action_config = data.action_config
    await session.flush()
    return InboxResponse(**inbox.to_dict())


@product_email_router.delete("/{product_id}/inbox", status_code=204)
async def delete_inbox(
    product_id: str, session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(EmailInbox).where(EmailInbox.product_id == product_id)
    )
    inbox = result.scalar_one_or_none()
    if not inbox:
        raise HTTPException(404, "No inbox for this product")
    await session.delete(inbox)
    await session.flush()


# ── Inbound webhook receiver ─────────────────────────────────────────


@router.post("/inbound")
async def inbound_email_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """Receives parsed inbound email from any provider webhook.

    For generic/manual integration, accepts a JSON body:
    {to, from, subject, body_text, body_html?, provider_message_id?}
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    email = InboundEmail(
        provider_message_id=payload.get("provider_message_id", str(uuid4())),
        from_address=payload.get("from", ""),
        to_address=payload.get("to", ""),
        subject=payload.get("subject", ""),
        body_text=payload.get("body_text", ""),
        body_html=payload.get("body_html"),
    )

    msg = await email_processor.process(email, session)
    if msg:
        return {"status": "processed", "message_id": msg.message_id, "result_status": msg.status}
    return {"status": "skipped", "reason": "no matching inbox or inactive"}


# ── Drafts (Draft & Review mode) ─────────────────────────────────────


@product_email_router.get("/{product_id}/inbox/drafts", response_model=list[MessageResponse])
async def list_drafts(
    product_id: str, session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(EmailMessage)
        .where(
            EmailMessage.product_id == product_id,
            EmailMessage.status == "drafted",
        )
        .order_by(EmailMessage.received_at.desc())
    )
    return [MessageResponse(**m.to_dict()) for m in result.scalars().all()]


def _normalize_to_email(addr: str) -> str:
    if not addr:
        return ""
    addr = addr.replace("&lt;", "<").replace("&gt;", ">").strip().lower()
    m = _EMAIL_IN_ADDR_RE.search(addr)
    return m.group(1).strip().lower() if m else addr


@product_email_router.get("/{product_id}/inbox/unread", response_model=list[UnreadMessageResponse])
async def list_unread(
    product_id: str, session: AsyncSession = Depends(get_session)
):
    """Return the 5 most recent unread emails for this product's inbox, regardless of age."""
    result = await session.execute(
        select(EmailInbox).where(EmailInbox.product_id == product_id)
    )
    inbox = result.scalar_one_or_none()
    if not inbox:
        logger.warning("list_unread_no_inbox", product_id=product_id)
        return []
    conn = await session.get(EmailConnection, inbox.connection_id)
    if not conn:
        logger.warning("list_unread_no_connection", connection_id=inbox.connection_id)
        return []

    token_data = json.loads(conn.oauth_token_encrypted or "{}")
    access_token = token_data.get("access_token", "")
    client_id = getattr(conn, "client_id", None) or ""
    client_secret = getattr(conn, "client_secret", None) or ""

    logger.info("list_unread_start", product_id=product_id, provider=conn.provider, has_token=bool(access_token))

    if conn.provider == "zoho":
        from app.services.email.zoho import ZohoProvider
        zoho = ZohoProvider(client_id=client_id, client_secret=client_secret)

        async def _fetch_zoho(token: str):
            return await zoho.fetch_unread_emails(access_token=token, limit=5)

        try:
            raw_msgs = await _fetch_zoho(access_token)
        except Exception as exc:
            err_str = str(exc)
            logger.error("list_unread_fetch_error", error=err_str)
            is_401 = "401" in err_str
            refresh_token = token_data.get("refresh_token", "")
            if is_401 and refresh_token and client_id and client_secret:
                try:
                    new_tokens = await zoho.refresh_token(refresh_token)
                    token_data["access_token"] = new_tokens["access_token"]
                    if new_tokens.get("refresh_token"):
                        token_data["refresh_token"] = new_tokens["refresh_token"]
                    conn.oauth_token_encrypted = json.dumps(token_data)
                    await session.flush()
                    raw_msgs = await _fetch_zoho(token_data["access_token"])
                except Exception as refresh_exc:
                    logger.error("list_unread_refresh_failed", error=str(refresh_exc))
                    raise HTTPException(401, "Token expired. Reconnect the email provider in Settings.")
            else:
                raise HTTPException(400, f"Failed to fetch unread: {exc}")

        logger.info("list_unread_results", count=len(raw_msgs))
        return [UnreadMessageResponse(**m) for m in raw_msgs]

    # Fallback for other providers: use fetch_new_messages with far-past since
    provider = get_provider(conn, client_id=client_id, client_secret=client_secret, tenant_id="common")
    since = datetime(2000, 1, 1)
    messages = await provider.fetch_new_messages(access_token=access_token, since=since)
    out: list[UnreadMessageResponse] = []
    for email in messages:
        out.append(
            UnreadMessageResponse(
                from_address=email.from_address or "",
                subject=email.subject or "",
                received_at=email.received_at.isoformat() if email.received_at else None,
                body_text=(email.body_text or "")[:500],
            )
        )
        if len(out) >= 5:
            break
    return out


@product_email_router.post("/{product_id}/inbox/drafts/{message_id}/approve")
async def approve_draft(
    product_id: str,
    message_id: str,
    data: Optional[DraftAction] = None,
    session: AsyncSession = Depends(get_session),
):
    msg = await session.get(EmailMessage, message_id)
    if not msg or msg.product_id != product_id or msg.status != "drafted":
        raise HTTPException(404, "Draft not found")

    response_text = (data.edited_response if data and data.edited_response else msg.ai_response) or ""

    # Send the reply
    inbox_result = await session.execute(
        select(EmailInbox).where(EmailInbox.inbox_id == msg.inbox_id)
    )
    inbox = inbox_result.scalar_one_or_none()
    if inbox:
        conn = await session.get(EmailConnection, inbox.connection_id)
        if conn:
            try:
                token_data = json.loads(conn.oauth_token_encrypted or "{}")
                provider = get_provider(conn)
                reply_html = (
                    f"<div>{response_text}</div>"
                    f"<br><hr><small>This response was generated by AI and reviewed by a human.</small>"
                )
                await provider.send_reply(
                    access_token=token_data.get("access_token", ""),
                    to=msg.from_address,
                    subject=f"Re: {msg.subject}",
                    body_html=reply_html,
                    in_reply_to=msg.provider_message_id,
                )
            except Exception as exc:
                logger.error("draft_approve_send_failed", error=str(exc))
                raise HTTPException(500, f"Failed to send reply: {exc}")

    msg.ai_response = response_text
    msg.status = "approved"
    await session.flush()
    return {"status": "approved", "message_id": message_id}


@product_email_router.post("/{product_id}/inbox/drafts/{message_id}/reject")
async def reject_draft(
    product_id: str,
    message_id: str,
    session: AsyncSession = Depends(get_session),
):
    msg = await session.get(EmailMessage, message_id)
    if not msg or msg.product_id != product_id or msg.status != "drafted":
        raise HTTPException(404, "Draft not found")
    msg.status = "rejected"
    await session.flush()
    return {"status": "rejected", "message_id": message_id}


@product_email_router.patch("/{product_id}/inbox/drafts/{message_id}")
async def edit_draft(
    product_id: str,
    message_id: str,
    data: DraftAction,
    session: AsyncSession = Depends(get_session),
):
    msg = await session.get(EmailMessage, message_id)
    if not msg or msg.product_id != product_id or msg.status != "drafted":
        raise HTTPException(404, "Draft not found")
    if data.edited_response is not None:
        msg.ai_response = data.edited_response
    await session.flush()
    return {"status": "updated", "message_id": message_id}


# ── Message log ───────────────────────────────────────────────────────


@product_email_router.get("/{product_id}/inbox/messages", response_model=list[MessageResponse])
async def list_messages(
    product_id: str,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(EmailMessage)
        .where(EmailMessage.product_id == product_id)
        .order_by(EmailMessage.received_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [MessageResponse(**m.to_dict()) for m in result.scalars().all()]
