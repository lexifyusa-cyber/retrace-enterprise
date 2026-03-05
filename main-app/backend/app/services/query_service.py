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
        """Return (llm_settings, embedding_index, llm_client, model, classifier)."""
        from app.api.settings import get_active_llm_settings
        from app.rag.clients import OpenAIEmbeddingClient
        from app.rag.embedding_index import EmbeddingIndexService
        from app.rag.query_classifier import QueryClassifier

        llm = await get_active_llm_settings(session)

        emb_key = llm.get("embeddings_api_key") or llm.get("api_key")
        if not emb_key:
            raise RuntimeError("No embedding API key configured. Set one in Settings.")

        if llm.get("embeddings_api_key"):
            emb_base_url = llm.get("embeddings_api_url") or None
        else:
            emb_base_url = llm.get("embeddings_api_url") or llm.get("api_url") or None

        embedding_client = OpenAIEmbeddingClient(
            api_key=emb_key,
            base_url=emb_base_url,
            model=llm.get("embeddings_model") or "text-embedding-3-small",
        )
        index = EmbeddingIndexService(embedding_client=embedding_client)
        llm_client = self._build_llm_client(llm)
        model = llm.get("model_name") or settings.REASONING_MODEL

        classifier = QueryClassifier(client=llm_client, model=model)

        return llm, index, llm_client, model, classifier

    # ------------------------------------------------------------------
    # Multi-pass search
    # ------------------------------------------------------------------

    async def _routed_search(self, index, product_ids, question, plan):
        """Execute multi-pass search based on the query plan.

        Pass 1 – primary types + sub-categories (high top_k, component-filtered)
        Pass 2 – primary types without sub-category filter (catch broader matches)
        Pass 3 – secondary types (lower top_k, broader)
        Pass 4 – summary chunks (always, for context)
        De-duplicate by chunk ID.
        """
        seen_ids: set[str] = set()
        all_chunks = []

        async def _search(types, top_k, component=None, sub_categories=None, label=""):
            for pid in product_ids:
                filters: dict = {}
                if types:
                    filters["processing_types"] = types
                if component:
                    filters["component"] = component
                if sub_categories:
                    filters["sub_categories"] = sub_categories

                for query_text in plan.search_queries:
                    chunks = await index.search(
                        product_id=pid,
                        query=query_text,
                        top_k=top_k,
                        filters=filters if filters else None,
                    )
                    for c in chunks:
                        if c.id not in seen_ids:
                            seen_ids.add(c.id)
                            all_chunks.append(c)

        primary = plan.primary_types or ["summary", "code", "doc"]
        component = plan.component if plan.component else None
        sub_cats = plan.sub_categories if plan.sub_categories else None
        primary_k = 8 if plan.strategy == "specific" else 12

        # Pass 1: primary types + sub-categories + component (most targeted)
        if sub_cats:
            await _search(primary, primary_k, component=component, sub_categories=sub_cats, label="primary_targeted")

        # Pass 2: primary types + component, no sub-category filter (broader)
        await _search(primary, primary_k, component=component, sub_categories=None, label="primary_broad")

        # Pass 3: secondary types, no component filter
        if plan.secondary_types:
            await _search(plan.secondary_types, 5, component=None, label="secondary")

        # Pass 4: always grab summaries (for context)
        if "summary" not in primary and "summary" not in (plan.secondary_types or []):
            await _search(["summary"], 5, component=component, label="summary_fallback")

        # Sort by score descending
        all_chunks.sort(key=lambda c: c.metadata.score or 0, reverse=True)

        return all_chunks

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
        llm, index, llm_client, model, classifier = await self._init_clients(session)
        timings["init_clients_ms"] = _ms_since(t0)

        # 1. Classify
        t0 = time.time()
        components = await classifier.get_available_components(product_ids)
        plan = await classifier.classify(question, available_components=components)
        timings["classify_ms"] = _ms_since(t0)

        # 2. Multi-pass search
        t0 = time.time()
        all_chunks = await self._routed_search(index, product_ids, question, plan)
        timings["vector_search_ms"] = _ms_since(t0)

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
            llm, index, llm_client, model, classifier = await self._init_clients(session)
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

            # 2. Multi-pass search
            search_msg = f"Searching {', '.join(plan.primary_types)}"
            if plan.component:
                search_msg += f" in '{plan.component}'"
            yield _sse("status", {"step": "search", "message": search_msg + "..."})
            t0 = time.time()
            all_chunks = await self._routed_search(index, product_ids, question, plan)
            timings["vector_search_ms"] = _ms_since(t0)
            yield _sse("timings", {"vector_search_ms": timings["vector_search_ms"]})

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
