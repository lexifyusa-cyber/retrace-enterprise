"""
Agent API endpoints — SSE streaming for CodeAct agent execution,
conversation persistence, and Learn This (Q&A training).
"""

from datetime import datetime
from typing import Optional, List
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import asyncio
import structlog

from app.db.database import get_session, async_session_maker
from app.models.agent_session import AgentSession
from app.models.conversation import Conversation, ConversationMessage
from app.models.sop import SOP
from app.services.agent_service import agent_service

logger = structlog.get_logger()
router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class AgentExecuteRequest(BaseModel):
    product_id: Optional[str] = Field(default=None)
    task: str = Field(..., min_length=1)
    max_iterations: int = Field(default=10, ge=1, le=30)
    conversation_id: Optional[str] = None


class AgentSessionResponse(BaseModel):
    session_id: str
    product_id: str
    task: str
    status: str
    iterations: int
    final_answer: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class AgentToolInfo(BaseModel):
    name: str
    description: str
    requires_key: Optional[str] = None
    available: bool = True
    enabled: bool = True


# ── Conversation schemas ─────────────────────────────────────────────────

class ConversationCreateRequest(BaseModel):
    product_id: Optional[str] = Field(default=None)
    title: str = Field(default="New conversation", max_length=255)


class ConversationRenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)


class MessageSaveRequest(BaseModel):
    """Bulk-save messages for a conversation (used after agent execution)."""
    messages: List[dict]


# ── Learn This schemas ───────────────────────────────────────────────────

class LearnThisRequest(BaseModel):
    """Train a Q&A pair from an agent conversation."""
    product_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)


# ── SOP schemas ──────────────────────────────────────────────────────────

class CreateSOPRequest(BaseModel):
    """Create an SOP from current conversation context."""
    product_id: Optional[str] = Field(default=None)
    goal: str = Field(..., min_length=1)
    messages: List[dict] = Field(..., min_length=1)
    conversation_id: Optional[str] = None


class EditSOPRequest(BaseModel):
    """Edit an existing SOP with user-described changes."""
    sop_id: str = Field(..., min_length=1)
    edit_instructions: str = Field(..., min_length=1)


class ApproveAutomationRequest(BaseModel):
    """Approve an automation with optional scheduling."""
    schedule_type: str = Field(default="none")  # none, once, interval, daily, weekly, monthly, cron
    schedule_config: Optional[dict] = None


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------

@router.post("/execute")
async def execute_agent_task(
    request: AgentExecuteRequest,
    session: AsyncSession = Depends(get_session),
):
    """Start an agent task execution. Returns an SSE stream."""
    logger.info(
        "agent_execute_start",
        product_id=request.product_id,
        task=request.task[:80],
        max_iterations=request.max_iterations,
    )

    async def event_generator():
        async for event in agent_service.execute_task(
            product_id=request.product_id,
            task=request.task,
            session=session,
            max_iterations=request.max_iterations,
            thread_id=request.conversation_id,
        ):
            yield event

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Agent sessions (execution history)
# ---------------------------------------------------------------------------

@router.get("/sessions")
async def list_agent_sessions(
    product_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    """List past agent sessions, newest first."""
    q = select(AgentSession).order_by(AgentSession.created_at.desc()).limit(limit)
    if product_id:
        q = q.where(AgentSession.product_id == product_id)

    result = await session.execute(q)
    sessions = result.scalars().all()
    return [s.to_dict() for s in sessions]


@router.get("/sessions/{session_id}")
async def get_agent_session(
    session_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Get a specific agent session."""
    result = await session.execute(
        select(AgentSession).where(AgentSession.session_id == session_id)
    )
    agent_session = result.scalar_one_or_none()
    if not agent_session:
        raise HTTPException(status_code=404, detail="Session not found")
    return agent_session.to_dict()


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------

@router.get("/tools")
async def list_agent_tools(
    session: AsyncSession = Depends(get_session),
):
    """List available agent tools and their status."""
    import json
    from app.api.settings import get_active_llm_settings

    llm = await get_active_llm_settings(session)
    has_screenops_key = bool(llm.get("screenops_api_key"))
    # ScreenOps also needs pyautogui/Pillow; tool builder returns None if import fails
    try:
        from app.tools.screenops.execution import pyautogui, Image
        has_screenops_deps = pyautogui is not None and Image is not None
    except Exception:
        has_screenops_deps = False
    screenops_available = has_screenops_key and has_screenops_deps

    # Load disabled tools from saved config
    disabled_set: set[str] = set()
    config = llm.get("agent_tools_config")
    if config and isinstance(config, dict):
        disabled_set = set(config.get("disabled", []))

    tools = [
        AgentToolInfo(name="terminal", description="Execute shell/terminal commands", available=True),
        AgentToolInfo(name="read_file", description="Read file contents from disk", available=True),
        AgentToolInfo(name="write_file", description="Write text content to a file", available=True),
        AgentToolInfo(name="basic_search", description="Search the web for quick information", available=True),
        AgentToolInfo(name="deep_websearch", description="Comprehensive multi-source web research", available=bool(llm.get("api_key"))),
        AgentToolInfo(name="screenops", description="Screen-based computer automation (click, type, press)", requires_key="screenops_api_key", available=screenops_available),
        AgentToolInfo(name="search_knowledge_base", description="Search the product's trained knowledge base for information", available=True),
    ]

    for t in tools:
        if t.name in disabled_set:
            t.enabled = False

    return [t.model_dump() for t in tools]


# ---------------------------------------------------------------------------
# Conversations — persistent per-product chat threads
# ---------------------------------------------------------------------------

@router.get("/conversations")
async def list_conversations(
    product_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """List conversations, optionally filtered by product. product_id=__none__ returns product-less conversations."""
    q = (
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .order_by(Conversation.updated_at.desc())
    )
    if product_id == "__none__":
        q = q.where(Conversation.product_id.is_(None))
    elif product_id:
        q = q.where(Conversation.product_id == product_id)
    result = await session.execute(q)
    convos = result.scalars().all()
    return [c.to_dict(include_messages=False) for c in convos]


@router.post("/conversations")
async def create_conversation(
    data: ConversationCreateRequest,
    session: AsyncSession = Depends(get_session),
):
    """Create a new conversation for a product."""
    convo = Conversation(
        conversation_id=str(uuid4()),
        product_id=data.product_id,
        title=data.title,
    )
    session.add(convo)
    await session.flush()

    result = await session.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.conversation_id == convo.conversation_id)
    )
    convo = result.scalar_one()
    return convo.to_dict(include_messages=True)


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Get a conversation with all its messages."""
    result = await session.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.conversation_id == conversation_id)
    )
    convo = result.scalar_one_or_none()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return convo.to_dict(include_messages=True)


@router.put("/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: str,
    data: ConversationRenameRequest,
    session: AsyncSession = Depends(get_session),
):
    """Rename a conversation."""
    result = await session.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.conversation_id == conversation_id)
    )
    convo = result.scalar_one_or_none()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")
    convo.title = data.title
    convo.updated_at = datetime.utcnow()
    await session.flush()
    return convo.to_dict(include_messages=False)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Delete a conversation and all its messages."""
    result = await session.execute(
        select(Conversation).where(Conversation.conversation_id == conversation_id)
    )
    convo = result.scalar_one_or_none()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await session.delete(convo)
    return {"status": "deleted", "conversation_id": conversation_id}


@router.post("/conversations/{conversation_id}/messages")
async def save_messages(
    conversation_id: str,
    data: MessageSaveRequest,
    session: AsyncSession = Depends(get_session),
):
    """Bulk save messages to a conversation (appends to existing messages)."""
    result = await session.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.conversation_id == conversation_id)
    )
    convo = result.scalar_one_or_none()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")

    max_pos = max((m.position for m in convo.messages), default=-1)

    for i, msg in enumerate(data.messages):
        convo.messages.append(ConversationMessage(
            message_id=str(uuid4()),
            conversation_id=conversation_id,
            msg_type=msg.get("type", "user"),
            content=msg.get("content", ""),
            iteration=msg.get("iteration"),
            meta_json=msg.get("meta"),
            position=max_pos + 1 + i,
        ))

    convo.updated_at = datetime.utcnow()

    if convo.title == "New conversation":
        first_user = next((m for m in data.messages if m.get("type") == "user"), None)
        if first_user:
            title = first_user["content"][:80]
            if len(first_user["content"]) > 80:
                title += "..."
            convo.title = title

    await session.flush()
    return convo.to_dict(include_messages=True)


@router.delete("/conversations/{conversation_id}/messages")
async def clear_messages(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Clear all messages from a conversation (keeps the conversation)."""
    result = await session.execute(
        select(Conversation).where(Conversation.conversation_id == conversation_id)
    )
    convo = result.scalar_one_or_none()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await session.execute(
        delete(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation_id
        )
    )
    return {"status": "cleared", "conversation_id": conversation_id}


# ---------------------------------------------------------------------------
# Learn This — train Q&A pairs as prioritized knowledge
# ---------------------------------------------------------------------------

@router.post("/learn")
async def learn_qa(
    data: LearnThisRequest,
    session: AsyncSession = Depends(get_session),
):
    """Train a Q&A pair as expert-verified knowledge."""
    from app.rag.models import Chunk, ChunkMetadata, ProcessingType
    from app.rag.embedding_index import EmbeddingIndexService
    from app.rag.clients import get_embedding_client
    from app.api.settings import get_active_llm_settings

    llm_settings = await get_active_llm_settings(session)
    emb_client = get_embedding_client(llm_settings)
    index_service = EmbeddingIndexService(emb_client)

    qa_text = f"Question: {data.question}\n\nAnswer: {data.answer}"

    chunk_id = str(uuid4())
    chunk = Chunk(
        id=chunk_id,
        source_path=f"expert_qa/{chunk_id[:8]}.md",
        processing_type=ProcessingType.EXPERT_QA,
        text=qa_text,
        metadata=ChunkMetadata(
            sub_category="expert_verified",
            concepts=_extract_key_phrases(data.question),
            component="agent_conversation",
            feature="learn_this",
            file_description=f"Expert Q&A: {data.question[:100]}",
        ),
    )

    indexed = await index_service.index_chunks(
        product_id=data.product_id,
        chunks=[chunk],
    )

    logger.info(
        "learn_this_saved",
        product_id=data.product_id,
        chunk_id=chunk_id,
        question_len=len(data.question),
        answer_len=len(data.answer),
    )

    return {"status": "learned", "chunk_id": chunk_id, "indexed": indexed}


# ---------------------------------------------------------------------------
# SOPs — Standard Operating Procedures
# ---------------------------------------------------------------------------

_SOP_SYSTEM_PROMPT = """\
You are an expert at creating Standard Operating Procedures (SOPs).
You will receive a goal and the previous conversation context (user questions, agent code, tool outputs, agent answers).

Create a detailed, step-by-step SOP that an automated agent can follow to accomplish the goal.

IMPORTANT RULES:
- Each step must specify which TOOL to use. Available tools: terminal, read_file, write_file, basic_search, deep_websearch, screenops, search_knowledge_base.
- Each step must have an exact command, path, or input (not vague instructions).
- Steps must be in the correct execution order.
- Include prerequisites (what must be true before starting).
- Include expected outcome (what success looks like).

You MUST respond with valid JSON in this exact format:
{
  "title": "Short descriptive title",
  "purpose": "1-2 sentence description of what this SOP accomplishes",
  "prerequisites": ["prerequisite 1", "prerequisite 2"],
  "steps": [
    {
      "number": 1,
      "action": "Short description of what this step does",
      "tool": "terminal",
      "command": "exact command or path or input to use",
      "expected_result": "What should happen after this step"
    }
  ],
  "expected_outcome": "What success looks like after all steps are completed"
}

Respond ONLY with the JSON object. No markdown, no explanation, no code fences.
"""


def _build_sop_context(messages: List[dict]) -> str:
    """Format conversation messages into a readable context string for the LLM."""
    parts: list[str] = []
    for msg in messages:
        msg_type = msg.get("type", "")
        content = msg.get("content", "")
        if not content:
            continue
        if msg_type == "user":
            parts.append(f"USER: {content}")
        elif msg_type == "code":
            parts.append(f"AGENT CODE:\n```\n{content}\n```")
        elif msg_type == "output":
            parts.append(f"TOOL OUTPUT:\n{content[:2000]}")
        elif msg_type == "answer":
            parts.append(f"AGENT ANSWER:\n{content}")
    return "\n\n".join(parts)


def _sop_json_to_markdown(sop: dict) -> str:
    """Convert structured SOP JSON to a fixed-format Markdown string."""
    lines: list[str] = []
    lines.append(f"# {sop.get('title', 'Untitled SOP')}")
    lines.append("")
    lines.append("## Purpose")
    lines.append(sop.get("purpose", ""))
    lines.append("")

    prereqs = sop.get("prerequisites", [])
    if prereqs:
        lines.append("## Prerequisites")
        for p in prereqs:
            lines.append(f"- {p}")
        lines.append("")

    lines.append("## Steps")
    lines.append("")
    for step in sop.get("steps", []):
        lines.append(f"### Step {step.get('number', '?')}: {step.get('action', '')}")
        lines.append(f"- **Tool:** `{step.get('tool', 'N/A')}`")
        cmd = step.get("command", "")
        if cmd:
            lines.append(f"- **Command/Input:** `{cmd}`")
        expected = step.get("expected_result", "")
        if expected:
            lines.append(f"- **Expected Result:** {expected}")
        lines.append("")

    lines.append("## Expected Outcome")
    lines.append(sop.get("expected_outcome", ""))
    return "\n".join(lines)


@router.post("/create-sop")
async def create_sop(
    data: CreateSOPRequest,
    session: AsyncSession = Depends(get_session),
):
    """Create an SOP from the current conversation context. Returns SSE stream."""
    import json as json_mod
    from openai import AsyncOpenAI
    from app.api.settings import get_active_llm_settings
    from app.models.sop import SOP

    def _sse(event: str, data_dict: dict) -> str:
        return f"event: {event}\ndata: {json_mod.dumps(data_dict)}\n\n"

    async def event_generator():
        llm_settings = await get_active_llm_settings(session)
        api_key = llm_settings.get("api_key")
        if not api_key:
            yield _sse("sop_error", {"detail": "No LLM API key configured. Set one in Settings."})
            return

        context = _build_sop_context(data.messages)
        if not context.strip():
            yield _sse("sop_error", {"detail": "No usable conversation context found."})
            return

        user_prompt = (
            f"GOAL: {data.goal}\n\n"
            f"CONVERSATION CONTEXT:\n{context}"
        )

        client_kw: dict = {"api_key": api_key}
        api_url = llm_settings.get("api_url")
        if api_url:
            client_kw["base_url"] = api_url
        llm_client = AsyncOpenAI(**client_kw)
        model = llm_settings.get("model_name", "gpt-4o")

        yield _sse("sop_status", {"message": "Generating Automation..."})

        try:
            response = await llm_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SOP_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=4000,
            )
            raw = response.choices[0].message.content or ""
        except Exception as exc:
            logger.error("sop_llm_call_failed", error=str(exc))
            yield _sse("sop_error", {"detail": f"LLM call failed: {exc}"})
            return

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            sop_data = json_mod.loads(cleaned)
        except json_mod.JSONDecodeError:
            logger.error("sop_json_parse_failed", raw=raw[:500])
            yield _sse("sop_error", {"detail": "Failed to parse SOP from LLM response."})
            return

        sop_markdown = _sop_json_to_markdown(sop_data)

        # Stream the markdown word by word
        words = sop_markdown.split(' ')
        total = len(words)
        chunk_size = 4
        for wi in range(0, total, chunk_size):
            partial = ' '.join(words[:wi + chunk_size])
            yield _sse("sop_chunk", {"chunk": partial, "done": False})
            await asyncio.sleep(0.02)
        yield _sse("sop_chunk", {"chunk": sop_markdown, "done": True})

        # Store SOP
        sop_id = str(uuid4())
        sop = SOP(
            sop_id=sop_id,
            product_id=data.product_id,
            title=sop_data.get("title", data.goal[:100]),
            goal=data.goal,
            sop_json=sop_data,
            sop_markdown=sop_markdown,
            source_conversation_id=data.conversation_id,
        )
        session.add(sop)
        await session.flush()

        logger.info("sop_created", sop_id=sop_id, product_id=data.product_id, title=sop.title)
        yield _sse("sop_done", {"sop_id": sop_id, "title": sop_data.get("title", data.goal[:100]), "sop_markdown": sop_markdown})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/sops")
async def list_sops(
    product_id: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """List all SOPs for a product, newest first."""
    result = await session.execute(
        select(SOP)
        .where(SOP.product_id == product_id)
        .order_by(SOP.created_at.desc())
    )
    sops = result.scalars().all()
    return [s.to_dict() for s in sops]


@router.get("/sops/{sop_id}")
async def get_sop(
    sop_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Get a specific SOP."""
    result = await session.execute(
        select(SOP).where(SOP.sop_id == sop_id)
    )
    sop = result.scalar_one_or_none()
    if not sop:
        raise HTTPException(status_code=404, detail="SOP not found")
    return sop.to_dict()


@router.delete("/sops/{sop_id}")
async def delete_sop(
    sop_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Delete an SOP."""
    result = await session.execute(
        select(SOP).where(SOP.sop_id == sop_id)
    )
    sop = result.scalar_one_or_none()
    if not sop:
        raise HTTPException(status_code=404, detail="SOP not found")
    await session.delete(sop)
    return {"status": "deleted", "sop_id": sop_id}


@router.put("/sops/{sop_id}/approve")
async def approve_sop(
    sop_id: str,
    data: ApproveAutomationRequest = ApproveAutomationRequest(),
    session: AsyncSession = Depends(get_session),
):
    """Approve an automation with optional scheduling."""
    result = await session.execute(
        select(SOP).where(SOP.sop_id == sop_id)
    )
    sop = result.scalar_one_or_none()
    if not sop:
        raise HTTPException(status_code=404, detail="Automation not found")

    from datetime import datetime as dt, timedelta

    sop.status = "approved"
    sop.updated_at = dt.utcnow()
    sop.schedule_type = data.schedule_type if data.schedule_type != "none" else None
    sop.schedule_config = data.schedule_config

    if data.schedule_type != "none" and data.schedule_config:
        sop.is_active = True
        # Calculate next_run_at based on schedule
        sop.next_run_at = _calculate_next_run(data.schedule_type, data.schedule_config)
    else:
        sop.is_active = False
        sop.next_run_at = None

    await session.flush()

    # Add/remove scheduler job
    from app.services.scheduler_service import automation_scheduler
    if data.schedule_type != "none" and data.schedule_config:
        automation_scheduler.add_automation_job(sop_id, sop.product_id, data.schedule_type, data.schedule_config)
    else:
        automation_scheduler.remove_automation_job(sop_id)

    logger.info("sop_approved", sop_id=sop_id, schedule_type=data.schedule_type)
    return sop.to_dict()


def _calculate_next_run(schedule_type: str, config: dict) -> "datetime":
    """Calculate the next run time based on schedule configuration."""
    from datetime import datetime as dt, timedelta

    now = dt.utcnow()

    if schedule_type == "once":
        run_at = config.get("run_at")
        if run_at:
            return dt.fromisoformat(run_at.replace("Z", "+00:00").replace("+00:00", ""))
        return now + timedelta(minutes=5)

    elif schedule_type == "interval":
        every = config.get("every", 60)
        unit = config.get("unit", "minutes")
        if unit == "minutes":
            return now + timedelta(minutes=every)
        elif unit == "hours":
            return now + timedelta(hours=every)
        elif unit == "days":
            return now + timedelta(days=every)

    elif schedule_type == "daily":
        time_str = config.get("time", "09:00")
        h, m = map(int, time_str.split(":"))
        next_run = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        return next_run

    elif schedule_type == "weekly":
        day_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
        days = config.get("days", ["monday"])
        time_str = config.get("time", "09:00")
        h, m = map(int, time_str.split(":"))
        target_days = sorted([day_map.get(d.lower(), 0) for d in days])
        for offset in range(1, 8):
            candidate = now + timedelta(days=offset)
            if candidate.weekday() in target_days:
                return candidate.replace(hour=h, minute=m, second=0, microsecond=0)

    elif schedule_type == "monthly":
        day_of_month = config.get("day_of_month", 1)
        time_str = config.get("time", "09:00")
        h, m = map(int, time_str.split(":"))
        next_run = now.replace(day=min(day_of_month, 28), hour=h, minute=m, second=0, microsecond=0)
        if next_run <= now:
            if now.month == 12:
                next_run = next_run.replace(year=now.year + 1, month=1)
            else:
                next_run = next_run.replace(month=now.month + 1)
        return next_run

    return now + timedelta(hours=1)



# ---------------------------------------------------------------------------
# Automation execution & scheduling endpoints
# ---------------------------------------------------------------------------

@router.post("/sops/{sop_id}/run")
async def manual_run_automation(
    sop_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Trigger a manual run of an automation."""
    from app.models.sop import SOP
    result = await session.execute(select(SOP).where(SOP.sop_id == sop_id))
    sop = result.scalar_one_or_none()
    if not sop:
        raise HTTPException(status_code=404, detail="Automation not found")
    if not sop.sop_json:
        raise HTTPException(status_code=400, detail="Automation has no steps to execute")

    from app.services.scheduler_service import automation_scheduler
    await automation_scheduler.run_now(sop_id, sop.product_id)
    return {"status": "started", "sop_id": sop_id}


@router.get("/sops/{sop_id}/runs")
async def list_automation_runs(
    sop_id: str,
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    """List execution history for an automation."""
    from app.models.automation_run import AutomationRun
    result = await session.execute(
        select(AutomationRun)
        .where(AutomationRun.sop_id == sop_id)
        .order_by(AutomationRun.started_at.desc())
        .limit(limit)
    )
    runs = result.scalars().all()
    return [r.to_dict() for r in runs]


@router.put("/sops/{sop_id}/schedule")
async def update_schedule(
    sop_id: str,
    data: ApproveAutomationRequest,
    session: AsyncSession = Depends(get_session),
):
    """Update the schedule for an approved automation."""
    from app.models.sop import SOP
    result = await session.execute(select(SOP).where(SOP.sop_id == sop_id))
    sop = result.scalar_one_or_none()
    if not sop:
        raise HTTPException(status_code=404, detail="Automation not found")

    from datetime import datetime as dt
    sop.schedule_type = data.schedule_type if data.schedule_type != "none" else None
    sop.schedule_config = data.schedule_config
    sop.updated_at = dt.utcnow()

    if data.schedule_type != "none" and data.schedule_config:
        sop.is_active = True
        sop.next_run_at = _calculate_next_run(data.schedule_type, data.schedule_config)
    else:
        sop.is_active = False
        sop.next_run_at = None

    await session.flush()

    from app.services.scheduler_service import automation_scheduler
    if data.schedule_type != "none" and data.schedule_config:
        automation_scheduler.add_automation_job(sop_id, sop.product_id, data.schedule_type, data.schedule_config)
    else:
        automation_scheduler.remove_automation_job(sop_id)

    return sop.to_dict()


_SOP_EDIT_SYSTEM_PROMPT = """\
You are an expert at editing Standard Operating Procedures (SOPs).
You will receive the current SOP as JSON and the user's requested edits.

Apply the requested changes to the SOP while preserving the existing structure and any steps that are not affected by the edits.

IMPORTANT RULES:
- Maintain the same JSON format as the original SOP.
- Renumber steps if steps are added or removed so they are sequential starting from 1.
- Each step must specify which TOOL to use. Available tools: terminal, read_file, write_file, basic_search, deep_websearch, screenops, search_knowledge_base.
- Each step must have an exact command, path, or input (not vague instructions).
- Keep prerequisites and expected_outcome up to date with the changes.

Respond ONLY with the complete updated JSON object. No markdown, no explanation, no code fences.
The JSON format:
{
  "title": "Short descriptive title",
  "purpose": "1-2 sentence description of what this SOP accomplishes",
  "prerequisites": ["prerequisite 1", "prerequisite 2"],
  "steps": [
    {
      "number": 1,
      "action": "Short description of what this step does",
      "tool": "terminal",
      "command": "exact command or path or input to use",
      "expected_result": "What should happen after this step"
    }
  ],
  "expected_outcome": "What success looks like after all steps are completed"
}
"""


@router.post("/edit-sop")
async def edit_sop(
    data: EditSOPRequest,
    session: AsyncSession = Depends(get_session),
):
    """Edit an existing SOP based on user instructions. Returns SSE stream."""
    import json as json_mod
    from openai import AsyncOpenAI
    from app.api.settings import get_active_llm_settings
    from datetime import datetime as dt

    def _sse(event: str, data_dict: dict) -> str:
        return f"event: {event}\ndata: {json_mod.dumps(data_dict)}\n\n"

    async def event_generator():
        result = await session.execute(
            select(SOP).where(SOP.sop_id == data.sop_id)
        )
        sop = result.scalar_one_or_none()
        if not sop:
            yield _sse("sop_error", {"detail": "SOP not found"})
            return

        if not sop.sop_json:
            yield _sse("sop_error", {"detail": "SOP has no structured data to edit."})
            return

        llm_settings = await get_active_llm_settings(session)
        api_key = llm_settings.get("api_key")
        if not api_key:
            yield _sse("sop_error", {"detail": "No LLM API key configured. Set one in Settings."})
            return

        client_kw: dict = {"api_key": api_key}
        api_url = llm_settings.get("api_url")
        if api_url:
            client_kw["base_url"] = api_url
        llm_client = AsyncOpenAI(**client_kw)
        model = llm_settings.get("model_name", "gpt-4o")

        yield _sse("sop_status", {"message": "Editing Automation..."})

        user_prompt = (
            f"CURRENT SOP (JSON):\n{json_mod.dumps(sop.sop_json, indent=2)}\n\n"
            f"REQUESTED EDITS:\n{data.edit_instructions}"
        )

        try:
            response = await llm_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SOP_EDIT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=4000,
            )
            raw = response.choices[0].message.content or ""
        except Exception as exc:
            logger.error("sop_edit_llm_failed", error=str(exc))
            yield _sse("sop_error", {"detail": f"LLM call failed: {exc}"})
            return

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            updated_data = json_mod.loads(cleaned)
        except json_mod.JSONDecodeError:
            logger.error("sop_edit_parse_failed", raw=raw[:500])
            yield _sse("sop_error", {"detail": "Failed to parse edited SOP from LLM response."})
            return

        updated_markdown = _sop_json_to_markdown(updated_data)

        # Stream the updated markdown word by word
        words = updated_markdown.split(' ')
        total = len(words)
        chunk_size = 4
        for wi in range(0, total, chunk_size):
            partial = ' '.join(words[:wi + chunk_size])
            yield _sse("sop_chunk", {"chunk": partial, "done": False})
            await asyncio.sleep(0.02)
        yield _sse("sop_chunk", {"chunk": updated_markdown, "done": True})

        sop.sop_json = updated_data
        sop.sop_markdown = updated_markdown
        sop.title = updated_data.get("title", sop.title)
        sop.status = "draft"
        sop.updated_at = dt.utcnow()
        await session.flush()

        logger.info("sop_edited", sop_id=data.sop_id, title=sop.title)
        yield _sse("sop_done", {"sop_id": data.sop_id, "title": sop.title, "sop_markdown": updated_markdown})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


def _extract_key_phrases(text: str) -> str:
    """Extract simple key phrases from a question for metadata tagging."""
    stop_words = {
        "what", "how", "why", "when", "where", "which", "who", "is", "are",
        "was", "were", "do", "does", "did", "can", "could", "would", "should",
        "the", "a", "an", "in", "on", "at", "to", "for", "of", "with", "and",
        "or", "but", "not", "this", "that", "it", "be", "have", "has", "had",
        "will", "shall", "may", "might", "must", "about", "from", "by", "as",
        "if", "then", "than", "so", "also", "just", "only", "very", "too",
        "me", "my", "i", "you", "your", "we", "our", "they", "their",
    }
    words = text.lower().split()
    key_words = [w.strip("?.,!;:\"'()[]{}") for w in words if w.lower().strip("?.,!;:\"'()[]{}") not in stop_words]
    seen = set()
    unique = []
    for w in key_words:
        if w and w not in seen:
            seen.add(w)
            unique.append(w)
    return ", ".join(unique[:15])


# ---------------------------------------------------------------------------
# Documentation — Knowledge Articles from conversations
# ---------------------------------------------------------------------------

class CreateDocRequest(BaseModel):
    """Create a documentation article from current conversation context."""
    product_id: Optional[str] = Field(default=None)
    goal: str = Field(..., min_length=1)
    messages: List[dict] = Field(..., min_length=1)
    conversation_id: Optional[str] = None


class EditDocRequest(BaseModel):
    """Edit an existing documentation article with user-described changes."""
    doc_id: str = Field(..., min_length=1)
    edit_instructions: str = Field(..., min_length=1)


_DOC_SYSTEM_PROMPT = """\
You are an expert technical writer. You will receive:
1. A TOPIC (the user's original question).
2. The AGENT'S ANSWER — the final answer the agent gave to the user.

Your job: turn the agent's answer into a clean, polished Knowledge Article.

CRITICAL RULES:
- Document ONLY the substance of the agent's ANSWER. That is the content the user cares about.
- Do NOT mention tools, search engines, DuckDuckGo, log files, agent internals, or how the answer was produced.
- Do NOT reference the agent, the conversation, or "the system". Write as if this is original documentation.
- Focus purely on the SUBJECT MATTER — the actual information the user asked about.
- Preserve code snippets, commands, and technical details from the answer, but strip out any tool/search metadata.
- If the answer contains file paths or URLs that are relevant to the subject (not to agent logs), include them.

You MUST respond with valid JSON in this exact format:
{
  "title": "Clear descriptive title about the subject matter",
  "summary": "1-2 sentence abstract of the topic",
  "tags": ["tag1", "tag2", "tag3"],
  "sections": [
    {
      "heading": "Overview",
      "content": "Introduction to the topic..."
    },
    {
      "heading": "Details",
      "content": "In-depth explanation..."
    },
    {
      "heading": "Key Takeaways",
      "content": "Summary of important points..."
    }
  ],
  "references": ["only subject-relevant file paths or URLs from the answer"]
}

Guidelines:
- Always start with an "Overview" section.
- Add detail sections as needed based on the answer content.
- End with "Key Takeaways" summarizing the main points.
- Use markdown formatting (code blocks, lists, bold) within section content.
- Include 3-8 relevant tags about the SUBJECT, not about tools or agents.
- references should ONLY contain paths/URLs about the subject matter. Omit agent logs, tool configs, test files, etc.

Respond ONLY with the JSON object. No markdown, no explanation, no code fences.
"""


_DOC_EDIT_SYSTEM_PROMPT = """\
You are an expert technical writer editing a Knowledge Article.
You will receive the current article as JSON and the user's requested edits.

Apply the requested changes while preserving the existing structure and any sections that are not affected by the edits.

IMPORTANT RULES:
- Maintain the same JSON format as the original article.
- Keep all unaffected sections intact.
- Update tags if the content changes significantly.
- Update the summary if the overall topic shifts.
- Use markdown formatting within section content.

Respond ONLY with the complete updated JSON object. No markdown, no explanation, no code fences.
The JSON format:
{
  "title": "Clear descriptive title",
  "summary": "1-2 sentence abstract",
  "tags": ["tag1", "tag2"],
  "sections": [
    {"heading": "Section Title", "content": "Section content with markdown..."}
  ],
  "references": ["file paths or sources"]
}
"""


def _doc_json_to_markdown(doc: dict) -> str:
    """Convert structured documentation JSON to a Markdown string."""
    lines: list[str] = []
    lines.append(f"# {doc.get('title', 'Untitled Article')}")
    lines.append("")

    summary = doc.get("summary", "")
    if summary:
        lines.append(f"*{summary}*")
        lines.append("")

    tags = doc.get("tags", [])
    if tags:
        lines.append(" ".join(f"`{t}`" for t in tags))
        lines.append("")

    for section in doc.get("sections", []):
        lines.append(f"## {section.get('heading', 'Section')}")
        lines.append("")
        lines.append(section.get("content", ""))
        lines.append("")

    refs = doc.get("references", [])
    if refs:
        lines.append("## References")
        lines.append("")
        for r in refs:
            lines.append(f"- `{r}`")
        lines.append("")

    return "\n".join(lines)


def _build_doc_context(messages: list[dict]) -> str:
    """Extract only user questions and agent answers — no code, tool output, or internals."""
    parts: list[str] = []
    for msg in messages:
        msg_type = msg.get("type", "")
        content = msg.get("content", "")
        if not content:
            continue
        if msg_type == "user":
            parts.append(f"QUESTION: {content}")
        elif msg_type == "answer":
            parts.append(f"ANSWER:\n{content}")
    return "\n\n".join(parts)


@router.post("/create-doc")
async def create_doc(
    data: CreateDocRequest,
    session: AsyncSession = Depends(get_session),
):
    """Create a documentation article from the conversation's Q&A content. Returns SSE stream."""
    import json as json_mod
    from openai import AsyncOpenAI
    from app.api.settings import get_active_llm_settings
    from app.models.documentation import Documentation

    def _sse(event: str, data_dict: dict) -> str:
        return f"event: {event}\ndata: {json_mod.dumps(data_dict)}\n\n"

    async def event_generator():
        llm_settings = await get_active_llm_settings(session)
        api_key = llm_settings.get("api_key")
        if not api_key:
            yield _sse("doc_error", {"detail": "No LLM API key configured. Set one in Settings."})
            return

        # Only extract user questions + agent answers (no code/tool output/logs)
        doc_context = _build_doc_context(data.messages)
        if not doc_context.strip():
            yield _sse("doc_error", {"detail": "No usable conversation context found."})
            return

        # Guard: require at least one substantive answer
        answer_msgs = [m for m in data.messages if m.get("type") == "answer"]
        has_substantive = any(len(m.get("content", "")) > 80 for m in answer_msgs)
        if not has_substantive:
            yield _sse("doc_error", {
                "detail": "The conversation doesn't have enough substantive content to build documentation. "
                          "Ask the agent a detailed question first, then click Build Documentation on the answer."
            })
            return

        yield _sse("doc_status", {"message": "Generating Documentation..."})

        user_prompt = (
            f"TOPIC: {data.goal}\n\n"
            f"{doc_context}"
        )

        client_kw: dict = {"api_key": api_key}
        api_url = llm_settings.get("api_url")
        if api_url:
            client_kw["base_url"] = api_url
        llm_client = AsyncOpenAI(**client_kw)
        model = llm_settings.get("model_name", "gpt-4o")

        try:
            response = await llm_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _DOC_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=4000,
            )
            raw = response.choices[0].message.content or ""
        except Exception as exc:
            logger.error("doc_llm_call_failed", error=str(exc))
            yield _sse("doc_error", {"detail": f"LLM call failed: {exc}"})
            return

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            doc_data = json_mod.loads(cleaned)
        except json_mod.JSONDecodeError:
            logger.error("doc_json_parse_failed", raw=raw[:500])
            yield _sse("doc_error", {"detail": "Failed to parse documentation from LLM response."})
            return

        doc_markdown = _doc_json_to_markdown(doc_data)

        words = doc_markdown.split(' ')
        total = len(words)
        chunk_size = 4
        for wi in range(0, total, chunk_size):
            partial = ' '.join(words[:wi + chunk_size])
            yield _sse("doc_chunk", {"chunk": partial, "done": False})
            await asyncio.sleep(0.02)
        yield _sse("doc_chunk", {"chunk": doc_markdown, "done": True})

        doc_id = str(uuid4())
        doc = Documentation(
            doc_id=doc_id,
            product_id=data.product_id,
            title=doc_data.get("title", data.goal[:100]),
            goal=data.goal,
            doc_json=doc_data,
            doc_markdown=doc_markdown,
            source_conversation_id=data.conversation_id,
        )
        session.add(doc)
        await session.flush()

        logger.info("doc_created", doc_id=doc_id, product_id=data.product_id, title=doc.title)
        yield _sse("doc_done", {"doc_id": doc_id, "title": doc_data.get("title", data.goal[:100]), "doc_markdown": doc_markdown})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/docs")
async def list_docs(
    product_id: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """List all documentation articles for a product, newest first."""
    from app.models.documentation import Documentation
    result = await session.execute(
        select(Documentation)
        .where(Documentation.product_id == product_id)
        .order_by(Documentation.created_at.desc())
    )
    docs = result.scalars().all()
    return [d.to_dict() for d in docs]


@router.get("/docs/{doc_id}")
async def get_doc(
    doc_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Get a specific documentation article."""
    from app.models.documentation import Documentation
    result = await session.execute(
        select(Documentation).where(Documentation.doc_id == doc_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Documentation not found")
    return doc.to_dict()


@router.delete("/docs/{doc_id}")
async def delete_doc(
    doc_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Delete a documentation article."""
    from app.models.documentation import Documentation
    result = await session.execute(
        select(Documentation).where(Documentation.doc_id == doc_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Documentation not found")
    await session.delete(doc)
    return {"status": "deleted", "doc_id": doc_id}


@router.put("/docs/{doc_id}/approve")
async def approve_doc(
    doc_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Approve a documentation article (draft → approved)."""
    from app.models.documentation import Documentation
    from datetime import datetime as dt

    result = await session.execute(
        select(Documentation).where(Documentation.doc_id == doc_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Documentation not found")

    doc.status = "approved"
    doc.updated_at = dt.utcnow()
    await session.flush()
    return doc.to_dict()


@router.post("/edit-doc")
async def edit_doc(
    data: EditDocRequest,
    session: AsyncSession = Depends(get_session),
):
    """Edit an existing documentation article based on user instructions. Returns SSE stream."""
    import json as json_mod
    from openai import AsyncOpenAI
    from app.api.settings import get_active_llm_settings
    from app.models.documentation import Documentation
    from datetime import datetime as dt

    def _sse(event: str, data_dict: dict) -> str:
        return f"event: {event}\ndata: {json_mod.dumps(data_dict)}\n\n"

    async def event_generator():
        result = await session.execute(
            select(Documentation).where(Documentation.doc_id == data.doc_id)
        )
        doc = result.scalar_one_or_none()
        if not doc:
            yield _sse("doc_error", {"detail": "Documentation not found"})
            return

        if not doc.doc_json:
            yield _sse("doc_error", {"detail": "Documentation has no structured data to edit."})
            return

        llm_settings = await get_active_llm_settings(session)
        api_key = llm_settings.get("api_key")
        if not api_key:
            yield _sse("doc_error", {"detail": "No LLM API key configured. Set one in Settings."})
            return

        client_kw: dict = {"api_key": api_key}
        api_url = llm_settings.get("api_url")
        if api_url:
            client_kw["base_url"] = api_url
        llm_client = AsyncOpenAI(**client_kw)
        model = llm_settings.get("model_name", "gpt-4o")

        yield _sse("doc_status", {"message": "Editing Documentation..."})

        user_prompt = (
            f"CURRENT ARTICLE (JSON):\n{json_mod.dumps(doc.doc_json, indent=2)}\n\n"
            f"REQUESTED EDITS:\n{data.edit_instructions}"
        )

        try:
            response = await llm_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _DOC_EDIT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=4000,
            )
            raw = response.choices[0].message.content or ""
        except Exception as exc:
            logger.error("doc_edit_llm_failed", error=str(exc))
            yield _sse("doc_error", {"detail": f"LLM call failed: {exc}"})
            return

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            updated_data = json_mod.loads(cleaned)
        except json_mod.JSONDecodeError:
            logger.error("doc_edit_parse_failed", raw=raw[:500])
            yield _sse("doc_error", {"detail": "Failed to parse edited documentation from LLM response."})
            return

        updated_markdown = _doc_json_to_markdown(updated_data)

        words = updated_markdown.split(' ')
        total = len(words)
        chunk_size = 4
        for wi in range(0, total, chunk_size):
            partial = ' '.join(words[:wi + chunk_size])
            yield _sse("doc_chunk", {"chunk": partial, "done": False})
            await asyncio.sleep(0.02)
        yield _sse("doc_chunk", {"chunk": updated_markdown, "done": True})

        doc.doc_json = updated_data
        doc.doc_markdown = updated_markdown
        doc.title = updated_data.get("title", doc.title)
        doc.updated_at = dt.utcnow()
        await session.flush()

        logger.info("doc_edited", doc_id=data.doc_id, title=doc.title)
        yield _sse("doc_done", {"doc_id": data.doc_id, "title": doc.title, "doc_markdown": updated_markdown})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )



# ---------------------------------------------------------------------------
# Dashboard aggregate stats
# ---------------------------------------------------------------------------

@router.get("/dashboard-stats")
async def dashboard_stats(session: AsyncSession = Depends(get_session)):
    """Aggregate stats for the dashboard in a single call."""
    from app.models.sop import SOP
    from app.models.automation_run import AutomationRun
    from app.models.documentation import Documentation
    from app.models.product import Product
    from app.models.pod import Pod
    from app.models.conversation import Conversation
    from sqlalchemy import func
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())

    # Products (eager-load folder_groups so relationship is available in async context)
    products_result = await session.execute(
        select(Product).options(selectinload(Product.folder_groups))
    )
    products = products_result.scalars().all()
    total_groups = sum(len(p.folder_groups) for p in products)
    trained_groups = sum(
        sum(1 for g in p.folder_groups if g.training_status == "completed")
        for p in products
    )

    # PODs
    pods_result = await session.execute(select(Pod))
    pods = pods_result.scalars().all()
    online_pods = sum(1 for p in pods if p.status == "online")

    # Automations
    total_sops = (await session.execute(select(func.count(SOP.sop_id)))).scalar() or 0
    approved_sops = (await session.execute(
        select(func.count(SOP.sop_id)).where(SOP.status == "approved")
    )).scalar() or 0
    active_sops = (await session.execute(
        select(func.count(SOP.sop_id)).where(SOP.status == "approved", SOP.is_active == True)
    )).scalar() or 0

    # Automation runs
    total_runs = (await session.execute(select(func.count(AutomationRun.run_id)))).scalar() or 0
    completed_runs = (await session.execute(
        select(func.count(AutomationRun.run_id)).where(AutomationRun.status == "completed")
    )).scalar() or 0
    failed_runs = (await session.execute(
        select(func.count(AutomationRun.run_id)).where(AutomationRun.status == "failed")
    )).scalar() or 0

    # Recent runs (last 10)
    recent_runs_result = await session.execute(
        select(AutomationRun)
        .order_by(AutomationRun.started_at.desc())
        .limit(10)
    )
    recent_runs = recent_runs_result.scalars().all()

    # Map run sop_ids to titles
    run_sop_ids = list(set(r.sop_id for r in recent_runs))
    sop_titles = {}
    if run_sop_ids:
        sop_result = await session.execute(
            select(SOP.sop_id, SOP.title).where(SOP.sop_id.in_(run_sop_ids))
        )
        sop_titles = {row[0]: row[1] for row in sop_result.all()}

    # Documentation
    total_docs = (await session.execute(select(func.count(Documentation.doc_id)))).scalar() or 0
    approved_docs = (await session.execute(
        select(func.count(Documentation.doc_id)).where(Documentation.status == "approved")
    )).scalar() or 0
    docs_this_week = (await session.execute(
        select(func.count(Documentation.doc_id)).where(Documentation.created_at >= week_start)
    )).scalar() or 0

    # Conversations
    total_conversations = (await session.execute(select(func.count(Conversation.conversation_id)))).scalar() or 0
    conversations_today = (await session.execute(
        select(func.count(Conversation.conversation_id)).where(Conversation.created_at >= today_start)
    )).scalar() or 0

    # Upcoming scheduled runs
    upcoming_result = await session.execute(
        select(SOP)
        .where(SOP.status == "approved", SOP.is_active == True, SOP.next_run_at.isnot(None))
        .order_by(SOP.next_run_at.asc())
        .limit(5)
    )
    upcoming = upcoming_result.scalars().all()

    # Products with coverage data
    product_coverage = []
    for p in products:
        total = len(p.folder_groups)
        trained = sum(1 for g in p.folder_groups if g.training_status == "completed")
        in_progress = sum(1 for g in p.folder_groups if g.training_status == "training")
        product_coverage.append({
            "product_id": p.product_id,
            "product_name": p.product_name,
            "total_groups": total,
            "trained_groups": trained,
            "in_progress_groups": in_progress,
            "coverage_pct": round(trained / total * 100) if total > 0 else 0,
        })

    return {
        "stats": {
            "knowledge_coverage_pct": round(trained_groups / total_groups * 100) if total_groups > 0 else 0,
            "trained_groups": trained_groups,
            "total_groups": total_groups,
            "active_automations": active_sops,
            "approved_automations": approved_sops,
            "total_automations": total_sops,
            "total_docs": total_docs,
            "approved_docs": approved_docs,
            "docs_this_week": docs_this_week,
            "execution_success_rate": round(completed_runs / total_runs * 100) if total_runs > 0 else 100,
            "total_runs": total_runs,
            "completed_runs": completed_runs,
            "failed_runs": failed_runs,
            "online_pods": online_pods,
            "total_pods": len(pods),
            "total_conversations": total_conversations,
            "conversations_today": conversations_today,
            "total_products": len(products),
        },
        "recent_runs": [
            {
                **r.to_dict(),
                "sop_title": sop_titles.get(r.sop_id, "Unknown"),
            }
            for r in recent_runs
        ],
        "upcoming_schedules": [
            {
                "sop_id": s.sop_id,
                "title": s.title,
                "product_id": s.product_id,
                "schedule_type": s.schedule_type,
                "schedule_config": s.schedule_config,
                "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
            }
            for s in upcoming
        ],
        "product_coverage": product_coverage,
    }
