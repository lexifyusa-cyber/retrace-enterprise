"""
Team-assistant query API.

POST /api/v1/team-assistant/query
  → retrieve the most relevant knowledge for a question using
    LLM-expanded semantic search over the encrypted KB blob.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.db.database import get_session

logger = structlog.get_logger()
router = APIRouter()


# ── Request / Response schemas ────────────────────────────────────────────

class TeamAssistantQuery(BaseModel):
    question: str = Field(..., min_length=1, max_length=5000)
    team_id: str = Field(..., min_length=1)
    top_k: int = Field(default=10, ge=1, le=50)


class TeamAssistantResponse(BaseModel):
    result: str
    query: str


# ── Endpoint ──────────────────────────────────────────────────────────────

@router.post("/query", response_model=TeamAssistantResponse)
async def team_assistant_query(
    body: TeamAssistantQuery,
    session: AsyncSession = Depends(get_session),
):
    """Retrieve the most relevant knowledge for a question."""
    from app.api.settings import get_active_llm_settings
    from app.rag.kb_search import search_kb

    llm = await get_active_llm_settings(session)
    api_key = llm.get("api_key")
    if not api_key:
        raise HTTPException(status_code=400, detail="No LLM API key configured.")

    result = await search_kb(
        query=body.question,
        team_id=body.team_id,
        api_key=api_key,
        model=llm.get("model_name", "gpt-4o-mini"),
        base_url=llm.get("api_url"),
        top_k=body.top_k,
    )

    logger.info(
        "team_assistant_query",
        team_id=body.team_id,
        question=body.question[:80],
        result_len=len(result),
    )

    return TeamAssistantResponse(result=result, query=body.question)
