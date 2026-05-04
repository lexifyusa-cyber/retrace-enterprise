"""
Team description generator — uses LLM to generate team descriptions
based on trained knowledge base after first training completion.
"""

from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.models.team import Team
from app.api.settings import get_active_llm_settings
from app.rag.kb_search import search_kb
from app.core.config import settings

logger = structlog.get_logger()


async def generate_team_description(
    team_id: str,
    session: AsyncSession,
) -> Optional[str]:
    """Generate a team description using the trained knowledge base.

    Returns the generated description, or None if generation failed.
    Only generates if team.description is None or empty.
    """
    # Check if team already has a description
    result = await session.execute(
        select(Team).where(Team.team_id == team_id)
    )
    team = result.scalar_one_or_none()

    if not team:
        logger.warning("team_not_found_for_description", team_id=team_id)
        return None

    if team.description and team.description.strip():
        logger.info("team_already_has_description", team_id=team_id)
        return None

    try:
        # Get LLM settings
        llm_settings = await get_active_llm_settings(session)
        api_key = llm_settings.get("api_key")
        if not api_key:
            logger.warning("no_api_key_for_description_generation", team_id=team_id)
            return None

        # Search knowledge base (KB-based, no embeddings)
        context = await search_kb(
            query=f"What is {team.team_name}? Describe its purpose, features, and architecture.",
            team_id=team_id,
            api_key=api_key,
            model=llm_settings.get("model_name") or settings.REASONING_MODEL,
            base_url=llm_settings.get("api_url"),
            top_k=15,
        )

        if not context or "No knowledge base" in context or "No relevant" in context:
            logger.warning("no_chunks_found_for_description", team_id=team_id)
            return None

        # Generate description using LLM
        from openai import AsyncOpenAI

        client_kwargs: dict = {"api_key": api_key}
        if llm_settings.get("api_url"):
            client_kwargs["base_url"] = llm_settings["api_url"]

        client = AsyncOpenAI(**client_kwargs)
        model_name = llm_settings.get("model_name", settings.REASONING_MODEL)

        prompt = f"""Based on the following knowledge base about the team "{team.team_name}", generate a brief, professional team description (2-4 sentences).

The description should:
- Explain what the team is and its primary purpose
- Highlight key features or components
- Be concise and informative

Knowledge Base Context:
{context}

Generate only the description text, no additional commentary:"""

        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are a technical writer. Generate concise, accurate team descriptions based on technical documentation."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=300,
        )

        description = response.choices[0].message.content.strip()

        if description:
            # Update team with generated description
            await session.execute(
                update(Team)
                .where(Team.team_id == team_id)
                .values(description=description)
            )
            await session.commit()

            logger.info("team_description_generated", team_id=team_id, length=len(description))
            return description

        return None

    except Exception as exc:
        logger.error("team_description_generation_failed", team_id=team_id, error=str(exc))
        return None
