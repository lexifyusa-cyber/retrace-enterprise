"""
Query and Q&A API endpoints.
"""

import time
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.db.database import get_session
from app.models.query import QueryHistory
from app.services.query_service import query_service

logger = structlog.get_logger()
router = APIRouter()


class QueryRequest(BaseModel):
    """Schema for query request."""
    question: str = Field(..., min_length=1, max_length=5000)
    product_ids: list[str] = Field(
        ...,
        min_length=1,
        description="Product IDs to search for context",
    )


class SourceReference(BaseModel):
    """Schema for source reference."""
    type: str  # code, documentation, diagram, incident
    pod_id: str = ""
    group_name: str = ""
    file_path: Optional[str] = None
    snippet: Optional[str] = None
    metadata: dict = {}


class StepTimings(BaseModel):
    """Timing breakdown for each step of the query pipeline."""
    init_clients_ms: int = 0
    vector_search_ms: int = 0
    context_assembly_ms: int = 0
    llm_answer_ms: int = 0
    related_queries_ms: int = 0
    total_ms: int = 0


class QueryResponse(BaseModel):
    """Schema for query response."""
    query_id: str
    question: str
    answer: str
    confidence_score: float
    sources: list[SourceReference]
    related_queries: list[str]
    duration_ms: int
    timings: Optional[StepTimings] = None


class QueryHistoryResponse(BaseModel):
    """Schema for query history response."""
    query_id: str
    question: str
    answer: Optional[str]
    confidence_score: Optional[float]
    duration_ms: Optional[int]
    created_at: Optional[str]


@router.post("", response_model=QueryResponse)
async def ask_question(
    request: QueryRequest,
    session: AsyncSession = Depends(get_session)
):
    """
    Ask a natural language question scoped to selected products.

    Retrieves relevant chunks from the RAG index, assembles context,
    and generates an answer via the configured LLM.
    """
    start_time = time.time()
    query_id = str(uuid4())

    logger.info(
        "Processing query",
        query_id=query_id,
        question=request.question[:100],
        product_ids=request.product_ids,
    )

    try:
        result = await query_service.process_query(
            question=request.question,
            product_ids=request.product_ids,
            session=session,
        )

        duration_ms = int((time.time() - start_time) * 1000)

        # Save to history
        history = QueryHistory(
            query_id=query_id,
            question=request.question,
            scope_filter={"product_ids": request.product_ids},
            answer=result["answer"],
            confidence_score=result["confidence_score"],
            sources={
                "sources": [
                    s if isinstance(s, dict) else s.dict()
                    for s in result["sources"]
                ]
            },
            duration_ms=duration_ms,
        )
        session.add(history)
        await session.commit()

        logger.info(
            "Query completed",
            query_id=query_id,
            duration_ms=duration_ms,
            confidence=result["confidence_score"],
        )

        # Build step timings from pipeline result
        raw_timings = result.get("timings", {})
        step_timings = StepTimings(**raw_timings) if raw_timings else None

        return QueryResponse(
            query_id=query_id,
            question=request.question,
            answer=result["answer"],
            confidence_score=result["confidence_score"],
            sources=result["sources"],
            related_queries=result.get("related_queries", []),
            duration_ms=duration_ms,
            timings=step_timings,
        )

    except Exception as e:
        logger.error("Query failed", query_id=query_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stream")
async def ask_question_stream(
    request: QueryRequest,
    session: AsyncSession = Depends(get_session),
):
    """
    Streaming version of the query endpoint.

    Returns Server-Sent Events (SSE) with real-time updates:
      event: status  – pipeline step progress
      event: timings – timing for each step
      event: sources – retrieved sources
      event: token   – LLM answer tokens (streamed)
      event: related – related follow-up questions
      event: done    – final summary
      event: error   – if something goes wrong
    """
    logger.info(
        "Processing streaming query",
        question=request.question[:100],
        product_ids=request.product_ids,
    )

    async def event_generator():
        async for event in query_service.process_query_stream(
            question=request.question,
            product_ids=request.product_ids,
            session=session,
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


@router.get("/history", response_model=list[QueryHistoryResponse])
async def get_query_history(
    limit: int = 20,
    session: AsyncSession = Depends(get_session)
):
    """Get recent query history."""
    result = await session.execute(
        select(QueryHistory)
        .order_by(QueryHistory.created_at.desc())
        .limit(limit)
    )
    queries = result.scalars().all()

    return [
        QueryHistoryResponse(
            query_id=q.query_id,
            question=q.question,
            answer=q.answer[:200] + "..." if q.answer and len(q.answer) > 200 else q.answer,
            confidence_score=q.confidence_score,
            duration_ms=q.duration_ms,
            created_at=q.created_at.isoformat() if q.created_at else None,
        )
        for q in queries
    ]


@router.get("/{query_id}", response_model=QueryResponse)
async def get_query(
    query_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Get a specific query by ID."""
    result = await session.execute(
        select(QueryHistory).where(QueryHistory.query_id == query_id)
    )
    query = result.scalar_one_or_none()

    if not query:
        raise HTTPException(
            status_code=404,
            detail=f"Query {query_id} not found"
        )

    sources = query.sources.get("sources", []) if query.sources else []

    return QueryResponse(
        query_id=query.query_id,
        question=query.question,
        answer=query.answer or "",
        confidence_score=query.confidence_score or 0.0,
        sources=[SourceReference(**s) for s in sources],
        related_queries=[],
        duration_ms=query.duration_ms or 0,
    )
