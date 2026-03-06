"""
QueryClassifier – LLM-driven query routing.

Before performing vector search, the classifier analyses the user's question
and decides:
  • which content types to search (code, doc, tickets, summaries, etc.)
  • which component(s) are relevant
  • a rewritten search query optimised for embedding similarity
  • whether the question needs a broad overview or a specific answer
"""

import json
from typing import Optional

import structlog
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

logger = structlog.get_logger()

_CLASSIFIER_SYSTEM = """\
You are a query routing engine for a knowledge management system.

The system contains indexed knowledge about a software product organized into
these content types:
  • "code"              — source code chunks (functions, classes, modules)
  • "doc"               — documentation (markdown, PDF, docx, HTML)
  • "ticket_export"     — incident / issue tickets
  • "diagram_image"     — descriptions of architecture diagrams
  • "doc_with_diagrams" — documents containing embedded diagrams
  • "summary"           — auto-generated summaries of files and components
  • "other"             — anything else

Each content type has sub-categories:
  • code:              backend, frontend, api, database, tests, scripts, config-as-code, library, general
  • doc:               user-guide, api-docs, architecture, runbook, release-notes, tutorial, reference, general
  • ticket_export:     incident, bug, feature-request, change-request, general
  • diagram_image:     architecture, flow-diagram, sequence, network, er-diagram, ui-wireframe, general
  • doc_with_diagrams: architecture, design, general

Each chunk also carries optional metadata:
  • component — the subsystem it belongs to (e.g. "authentication", "payments")
  • concepts  — comma-separated technical concepts

Given a user's question, produce a routing plan as a JSON object:

{
  "primary_types": ["summary", "code"],
  "secondary_types": ["doc"],
  "sub_categories": ["user-guide", "backend"],
  "component": "authentication",
  "search_queries": ["original question rephrased for embedding search"],
  "strategy": "broad"
}

Field rules:
- **primary_types**: 1–3 content types to search first (highest priority).
  Always include "summary" if the question is about how something works,
  architecture, or overview topics.
- **secondary_types**: 0–2 additional types for supplementary context.
- **sub_categories**: 0–3 most relevant sub-categories to prioritize.
  E.g. for "how do I configure FTP?" → ["user-guide", "reference"].
  For "what caused the outage?" → ["incident"].
  Leave empty [] if no specific sub-category is relevant.
- **component**: the single most relevant component/subsystem, or "" if unclear.
- **search_queries**: 1–2 search queries optimized for semantic similarity.
  Rephrase the question into declarative statements that would match indexed text.
  Example: "how does auth work?" → ["authentication system implementation and flow"]
- **strategy**: "broad" for overview/architecture questions, "specific" for
  targeted lookups (e.g. "what does function X do?", "show me the DB schema").

Return ONLY the JSON object. No markdown, no explanation.
"""


class QueryPlan(BaseModel):
    """Structured output from the query classifier."""
    primary_types: list[str] = Field(default_factory=lambda: ["summary", "code", "doc"])
    secondary_types: list[str] = Field(default_factory=list)
    sub_categories: list[str] = Field(default_factory=list)
    component: str = ""
    search_queries: list[str] = Field(default_factory=list)
    strategy: str = "broad"  # "broad" or "specific"


class QueryClassifier:
    """Classify a user question into a search routing plan."""

    def __init__(self, client: AsyncOpenAI, model: str):
        self._client = client
        self._model = model

    async def classify(self, question: str, available_components: list[str] | None = None) -> QueryPlan:
        """Analyse *question* and return a ``QueryPlan``.

        *available_components* is an optional hint listing known component
        names so the LLM can pick from them.
        """
        user_msg = f"User question: {question}"
        if available_components:
            user_msg += f"\n\nKnown components: {', '.join(available_components[:30])}"

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _CLASSIFIER_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)

            plan = QueryPlan(
                primary_types=data.get("primary_types", ["summary", "code", "doc"]),
                secondary_types=data.get("secondary_types", []),
                sub_categories=data.get("sub_categories", []),
                component=data.get("component", ""),
                search_queries=data.get("search_queries", [question]),
                strategy=data.get("strategy", "broad"),
            )

            # Ensure search_queries always has at least the original question
            if not plan.search_queries:
                plan.search_queries = [question]

            logger.info(
                "query_classified",
                question=question[:80],
                primary_types=plan.primary_types,
                component=plan.component,
                strategy=plan.strategy,
            )
            return plan

        except Exception as exc:
            logger.warning("query_classification_failed", error=str(exc))
            # Fallback: search everything
            return QueryPlan(
                primary_types=["summary", "code", "doc", "ticket_export"],
                secondary_types=[],
                component="",
                search_queries=[question],
                strategy="broad",
            )

    async def get_available_components(self, product_ids: list[str]) -> list[str]:
        """Return distinct component values for the given products.

        Previously read from ChromaDB; now returns an empty list since
        embeddings have been removed.
        """
        return []
