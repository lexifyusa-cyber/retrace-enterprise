"""
Query service – retrieves context from RAG-indexed products and generates answers.

Pipeline:
  0. Init clients
  1. **Classify** the question (LLM-driven routing)
  2. **Multi-pass search** using the classification plan
  3. **Assemble** structured context
  4. **Generate** answer (LLM, optionally streamed)
  5. **Related** follow-up questions

Every step is timed so the caller can see where latency comes from.
Supports both blocking and SSE-streaming responses.
"""

import asyncio
import json
import time
from typing import Any, AsyncIterator, Optional

import structlog
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = structlog.get_logger()


def _ms_since(t0: float) -> int:
    """Milliseconds elapsed since *t0*."""
    return int((time.time() - t0) * 1000)


class QueryService:
    """Process natural language queries using RAG-indexed product data."""

    # ------------------------------------------------------------------
    # Helper: build clients once
    # ------------------------------------------------------------------

    async def _init_clients(self, session: AsyncSession):
        """Return (llm_settings, llm_client, model, classifier)."""
        from app.api.settings import get_active_llm_settings
        from app.rag.query_classifier import QueryClassifier

        llm = await get_active_llm_settings(session)
        api_key = llm.get("api_key")
        if not api_key:
            raise RuntimeError("No LLM API key configured. Set one in Settings.")

        llm_client = self._build_llm_client(llm)
        model = llm.get("model_name") or settings.REASONING_MODEL

        classifier = QueryClassifier(client=llm_client, model=model)

        return llm, llm_client, model, classifier

    # ------------------------------------------------------------------
    # Multi-pass search
    # ------------------------------------------------------------------

    async def _kb_search(self, llm, product_ids, question) -> list:
        """Execute KB search for all products in parallel and return chunk-like objects."""
        from types import SimpleNamespace
        from app.rag.kb_search import search_kb

        api_key = llm.get("api_key")
        model = llm.get("model_name") or settings.REASONING_MODEL
        base_url = llm.get("api_url") or None

        async def _search_one(pid: str):
            try:
                result = await search_kb(
                    query=question,
                    product_id=pid,
                    api_key=api_key,
                    model=model,
                    base_url=base_url,
                    top_k=15,
                )
                if result and "No knowledge base" not in result and "No relevant" not in result:
                    return SimpleNamespace(
                        id=f"kb_{pid}",
                        text=result,
                        source_path="knowledge_base",
                        processing_type=SimpleNamespace(value="doc"),
                        metadata=SimpleNamespace(
                            score=1.0,
                            component="",
                            model_dump=lambda exclude_none=True: {},
                        ),
                    )
            except Exception as exc:
                logger.warning("kb_search_failed", product_id=pid, error=str(exc))
            return None

        results = await asyncio.gather(*[_search_one(pid) for pid in product_ids])
        return [c for c in results if c is not None]

    # ------------------------------------------------------------------
    # Public: blocking query
    # ------------------------------------------------------------------

    async def process_query(
        self,
        question: str,
        product_ids: list[str],
        session: AsyncSession,
    ) -> dict[str, Any]:
        """Full pipeline with per-step timing."""
        timings: dict[str, int] = {}
        pipeline_start = time.time()

        # 0. Init
        t0 = time.time()
        llm, llm_client, model, classifier = await self._init_clients(session)
        timings["init_clients_ms"] = _ms_since(t0)

        # 1. Classify (for logging/plan; KB search uses question directly)
        t0 = time.time()
        components = await classifier.get_available_components(product_ids)
        plan = await classifier.classify(question, available_components=components)
        timings["classify_ms"] = _ms_since(t0)

        # 2. KB search
        t0 = time.time()
        all_chunks = await self._kb_search(llm, product_ids, question)
        timings["kb_search_ms"] = _ms_since(t0)

        logger.info(
            "query_retrieval_done",
            question=question[:80],
            products=len(product_ids),
            chunks=len(all_chunks),
            plan_primary=plan.primary_types,
            plan_component=plan.component,
        )

        # 3. Assemble context
        t0 = time.time()
        context = self._assemble_context(all_chunks)
        timings["context_assembly_ms"] = _ms_since(t0)

        # 4. Generate answer
        t0 = time.time()
        answer = await self._generate_answer(
            question=question,
            context=context,
            client=llm_client,
            model=model,
            plan=plan,
        )
        timings["llm_answer_ms"] = _ms_since(t0)

        # 5. Related queries
        t0 = time.time()
        related = await self._generate_related_queries(
            question, answer["answer_text"], llm_client, model
        )
        timings["related_queries_ms"] = _ms_since(t0)

        # Build result
        sources = self._build_sources(context)
        total = context["total_chunks"]
        confidence = min(0.95, 0.3 + total * 0.05) if total > 0 else 0.1
        timings["total_ms"] = _ms_since(pipeline_start)

        logger.info("query_pipeline_complete", timings=timings, chunks_found=total)

        return {
            "answer": answer["answer_text"],
            "confidence_score": round(confidence, 2),
            "sources": sources,
            "related_queries": related,
            "timings": timings,
        }

    # ------------------------------------------------------------------
    # Public: streaming query (SSE)
    # ------------------------------------------------------------------

    async def process_query_stream(
        self,
        question: str,
        product_ids: list[str],
        session: AsyncSession,
    ) -> AsyncIterator[str]:
        """Yields SSE events with real-time updates."""

        def _sse(event: str, data: Any) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        timings: dict[str, int] = {}
        pipeline_start = time.time()

        try:
            # 0. Init
            yield _sse("status", {"step": "init", "message": "Initializing..."})
            t0 = time.time()
            llm, llm_client, model, classifier = await self._init_clients(session)
            timings["init_clients_ms"] = _ms_since(t0)
            yield _sse("timings", {"init_clients_ms": timings["init_clients_ms"]})

            # 1. Classify
            yield _sse("status", {"step": "classify", "message": "Classifying question..."})
            t0 = time.time()
            components = await classifier.get_available_components(product_ids)
            plan = await classifier.classify(question, available_components=components)
            timings["classify_ms"] = _ms_since(t0)
            yield _sse("timings", {"classify_ms": timings["classify_ms"]})
            yield _sse("classification", {
                "primary_types": plan.primary_types,
                "sub_categories": plan.sub_categories,
                "component": plan.component,
                "strategy": plan.strategy,
            })

            # 2. KB search
            yield _sse("status", {"step": "search", "message": "Searching knowledge base..."})
            t0 = time.time()
            all_chunks = await self._kb_search(llm, product_ids, question)
            timings["kb_search_ms"] = _ms_since(t0)
            yield _sse("timings", {"kb_search_ms": timings["kb_search_ms"]})

            # 3. Assemble context
            yield _sse("status", {"step": "context", "message": f"Assembling context from {len(all_chunks)} chunks..."})
            t0 = time.time()
            context = self._assemble_context(all_chunks)
            sources = self._build_sources(context)
            timings["context_assembly_ms"] = _ms_since(t0)
            yield _sse("timings", {"context_assembly_ms": timings["context_assembly_ms"]})
            yield _sse("sources", sources)

            total_chunks = context["total_chunks"]
            confidence = min(0.95, 0.3 + total_chunks * 0.05) if total_chunks > 0 else 0.1

            # 4. Streaming LLM answer
            yield _sse("status", {"step": "answer", "message": "Generating answer..."})
            t0 = time.time()
            evidence = self._format_evidence(context)
            system = self._build_answer_system_prompt(plan)
            prompt = (
                f"QUESTION:\n{question}\n\n"
                f"EVIDENCE:\n{evidence}\n\n"
                "Provide a clear, comprehensive answer. Reference specific files or snippets where applicable."
            )

            full_answer = ""
            stream = await llm_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=3000,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    full_answer += delta.content
                    yield _sse("token", {"content": delta.content})

            timings["llm_answer_ms"] = _ms_since(t0)
            yield _sse("timings", {"llm_answer_ms": timings["llm_answer_ms"]})

            # 5. Related queries
            yield _sse("status", {"step": "related", "message": "Finding related questions..."})
            t0 = time.time()
            related = await self._generate_related_queries(question, full_answer, llm_client, model)
            timings["related_queries_ms"] = _ms_since(t0)
            yield _sse("timings", {"related_queries_ms": timings["related_queries_ms"]})
            yield _sse("related", related)

            # Done
            timings["total_ms"] = _ms_since(pipeline_start)
            yield _sse("done", {
                "confidence_score": round(confidence, 2),
                "timings": timings,
                "total_chunks": total_chunks,
                "answer_length": len(full_answer),
            })

            # Save to history
            try:
                from uuid import uuid4
                from app.models.query import QueryHistory
                query_id = str(uuid4())
                history = QueryHistory(
                    query_id=query_id,
                    question=question,
                    scope_filter={"product_ids": product_ids},
                    answer=full_answer,
                    confidence_score=round(confidence, 2),
                    sources={"sources": sources},
                    duration_ms=timings["total_ms"],
                )
                session.add(history)
                await session.commit()
            except Exception:
                logger.warning("stream_history_save_failed")

        except Exception as exc:
            logger.error("stream_query_failed", error=str(exc))
            yield _sse("error", {"detail": str(exc)})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_llm_client(llm: dict) -> AsyncOpenAI:
        kw: dict = {"api_key": llm["api_key"]}
        if llm.get("api_url"):
            kw["base_url"] = llm["api_url"]
        return AsyncOpenAI(**kw)

    @staticmethod
    def _build_answer_system_prompt(plan) -> str:
        """Build a system prompt tailored to the query strategy."""
        base = (
            "You are an expert knowledge assistant. You answer questions based on the "
            "evidence provided from the user's codebase, documentation, tickets, and diagrams. "
            "Cite file paths when relevant. If the evidence is insufficient, say so."
        )

        if plan.strategy == "broad":
            base += (
                "\n\nThe user is asking a broad/architectural question. "
                "Prioritize summary information and provide a cohesive overview. "
                "Connect different pieces of evidence into a unified explanation."
            )
        else:
            base += (
                "\n\nThe user is asking a specific/targeted question. "
                "Focus on the most directly relevant evidence. "
                "Provide precise, actionable details."
            )

        if plan.component:
            base += f"\n\nThe question is primarily about the '{plan.component}' component."

        return base

    @staticmethod
    def _assemble_context(chunks) -> dict:
        """Group retrieved chunks by processing type with priority ordering."""
        by_type: dict[str, list] = {
            "summary": [],
            "code": [],
            "doc": [],
            "ticket_export": [],
            "diagram_image": [],
            "doc_with_diagrams": [],
            "other": [],
        }

        for c in chunks:
            key = c.processing_type.value if hasattr(c.processing_type, "value") else str(c.processing_type)
            if key in by_type:
                by_type[key].append(c)
            else:
                by_type["other"].append(c)

        return {
            "by_type": by_type,
            "total_chunks": len(chunks),
        }

    async def _generate_answer(
        self,
        question: str,
        context: dict,
        client: AsyncOpenAI,
        model: str,
        plan=None,
    ) -> dict:
        evidence = self._format_evidence(context)
        system = self._build_answer_system_prompt(plan) if plan else (
            "You are an expert knowledge assistant. You answer questions based on the "
            "evidence provided from the user's codebase, documentation, tickets, and diagrams. "
            "Cite file paths when relevant. If the evidence is insufficient, say so."
        )
        prompt = (
            f"QUESTION:\n{question}\n\n"
            f"EVIDENCE:\n{evidence}\n\n"
            "Provide a clear, comprehensive answer. Reference specific files or snippets where applicable."
        )

        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=3000,
        )
        return {"answer_text": response.choices[0].message.content or ""}

    async def _generate_related_queries(
        self, question: str, answer: str, client: AsyncOpenAI, model: str
    ) -> list[str]:
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "Generate 3 related follow-up questions. Return only the questions, one per line.",
                    },
                    {
                        "role": "user",
                        "content": f"Question: {question}\n\nAnswer: {answer[:500]}",
                    },
                ],
                temperature=0.5,
                max_tokens=200,
            )
            text = response.choices[0].message.content or ""
            return [q.strip().lstrip("0123456789.-) ") for q in text.split("\n") if q.strip()][:3]
        except Exception:
            return []

    @staticmethod
    def _format_evidence(context: dict) -> str:
        """Format evidence with summaries first for better context."""
        # Priority order: summaries first, then code, docs, tickets, diagrams
        priority = ["summary", "code", "doc", "ticket_export", "diagram_image", "doc_with_diagrams", "other"]

        sections: list[str] = []
        for source_type in priority:
            chunks = context.get("by_type", {}).get(source_type, [])
            if not chunks:
                continue

            label = {
                "summary": "SUMMARIES & OVERVIEWS",
                "code": "SOURCE CODE",
                "doc": "DOCUMENTATION",
                "ticket_export": "INCIDENTS / TICKETS",
                "diagram_image": "ARCHITECTURE DIAGRAMS",
                "doc_with_diagrams": "DOCUMENTS WITH DIAGRAMS",
                "other": "OTHER",
            }.get(source_type, source_type.upper())

            section = f"\n### {label} ({len(chunks)} sources)\n"
            for i, chunk in enumerate(chunks[:5], 1):
                text = chunk.text[:600] if hasattr(chunk, "text") else str(chunk)[:600]
                path = chunk.source_path if hasattr(chunk, "source_path") else "unknown"
                component = chunk.metadata.component if hasattr(chunk.metadata, "component") and chunk.metadata.component else ""
                comp_tag = f" [{component}]" if component else ""
                section += f"\n[{i}] {path}{comp_tag}\n{text}\n"
            sections.append(section)

        return "\n".join(sections) if sections else "No relevant evidence found in the indexed knowledge base."

    @staticmethod
    def _build_sources(context: dict) -> list[dict]:
        sources: list[dict] = []
        for source_type, chunks in context.get("by_type", {}).items():
            for chunk in chunks[:3]:
                ptype = chunk.processing_type.value if hasattr(chunk.processing_type, "value") else str(chunk.processing_type)
                display_type = {
                    "code": "code",
                    "doc": "documentation",
                    "doc_with_diagrams": "documentation",
                    "ticket_export": "incident",
                    "diagram_image": "diagram",
                    "summary": "documentation",
                }.get(ptype, "documentation")

                meta = chunk.metadata.model_dump(exclude_none=True) if hasattr(chunk.metadata, "model_dump") else {}
                sources.append({
                    "type": display_type,
                    "pod_id": "",
                    "group_name": ptype,
                    "file_path": chunk.source_path if hasattr(chunk, "source_path") else None,
                    "snippet": chunk.text[:200] if hasattr(chunk, "text") else None,
                    "metadata": meta,
                })
        return sources


# Global instance
query_service = QueryService()
