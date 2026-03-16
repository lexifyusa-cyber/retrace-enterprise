"""
Product description generator — uses LLM to generate product descriptions
based on trained knowledge base after first training completion.
"""

from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.models.product import Product
from app.api.settings import get_active_llm_settings
from app.rag.kb_search import search_kb
from app.core.config import settings

logger = structlog.get_logger()


async def generate_product_description(
    product_id: str,
    session: AsyncSession,
) -> Optional[str]:
    """Generate a product description using the trained knowledge base.
    
    Returns the generated description, or None if generation failed.
    Only generates if product.description is None or empty.
    """
    # Check if product already has a description
    result = await session.execute(
        select(Product).where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()
    
    if not product:
        logger.warning("product_not_found_for_description", product_id=product_id)
        return None
    
    if product.description and product.description.strip():
        logger.info("product_already_has_description", product_id=product_id)
        return None
    
    try:
        # Get LLM settings
        llm_settings = await get_active_llm_settings(session)
        api_key = llm_settings.get("api_key")
        if not api_key:
            logger.warning("no_api_key_for_description_generation", product_id=product_id)
            return None
        
        # Search knowledge base (KB-based, no embeddings)
        context = await search_kb(
            query=f"What is {product.product_name}? Describe its purpose, features, and architecture.",
            product_id=product_id,
            api_key=api_key,
            model=llm_settings.get("model_name") or settings.REASONING_MODEL,
            base_url=llm_settings.get("api_url"),
            top_k=15,
        )
        
        if not context or "No knowledge base" in context or "No relevant" in context:
            logger.warning("no_chunks_found_for_description", product_id=product_id)
            return None
        
        # Generate description using LLM
        from openai import AsyncOpenAI
        
        client_kwargs: dict = {"api_key": api_key}
        if llm_settings.get("api_url"):
            client_kwargs["base_url"] = llm_settings["api_url"]
        
        client = AsyncOpenAI(**client_kwargs)
        model_name = llm_settings.get("model_name", settings.REASONING_MODEL)
        
        prompt = f"""Based on the following knowledge base about the product "{product.product_name}", generate a brief, professional product description (2-4 sentences).

The description should:
- Explain what the product is and its primary purpose
- Highlight key features or components
- Be concise and informative

Knowledge Base Context:
{context}

Generate only the description text, no additional commentary:"""
        
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are a technical writer. Generate concise, accurate product descriptions based on technical documentation."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=300,
        )
        
        description = response.choices[0].message.content.strip()
        
        if description:
            # Update product with generated description
            await session.execute(
                update(Product)
                .where(Product.product_id == product_id)
                .values(description=description)
            )
            await session.commit()
            
            logger.info("product_description_generated", product_id=product_id, length=len(description))
            return description
        
        return None
        
    except Exception as exc:
        logger.error("product_description_generation_failed", product_id=product_id, error=str(exc))
        return None
