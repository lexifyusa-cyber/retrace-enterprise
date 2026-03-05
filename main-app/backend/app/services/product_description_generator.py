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
from app.rag.clients import OpenAIEmbeddingClient
from app.rag.embedding_index import EmbeddingIndexService
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
        
        # Build embedding client to search knowledge base
        emb_key = llm_settings.get("embeddings_api_key") or api_key
        if not emb_key:
            logger.warning("no_embedding_key_for_description", product_id=product_id)
            return None
        
        emb_base = llm_settings.get("embeddings_api_url") or (
            llm_settings.get("api_url") if not llm_settings.get("embeddings_api_key") else None
        )
        
        embedding_client = OpenAIEmbeddingClient(
            api_key=emb_key,
            base_url=emb_base,
            model=llm_settings.get("embeddings_model") or "text-embedding-3-small",
        )
        index = EmbeddingIndexService(embedding_client=embedding_client)
        
        # Search for summary chunks and key information
        summary_chunks = await index.search(
            product_id=product_id,
            query=f"What is {product.product_name}? Describe its purpose, features, and architecture.",
            top_k=10,
            filters={"processing_types": ["summary"]},
        )
        
        # Also get some general chunks if summaries are sparse
        if len(summary_chunks) < 5:
            general_chunks = await index.search(
                product_id=product_id,
                query=f"Overview of {product.product_name} features and components",
                top_k=15,
            )
            # Combine, prioritizing summaries
            all_chunks = summary_chunks + [c for c in general_chunks if c not in summary_chunks]
        else:
            all_chunks = summary_chunks
        
        if not all_chunks:
            logger.warning("no_chunks_found_for_description", product_id=product_id)
            return None
        
        # Build context from chunks
        context_parts = []
        for chunk in all_chunks[:15]:  # Limit to top 15
            chunk_text = chunk.text[:800] if hasattr(chunk, "text") else str(chunk)[:800]
            context_parts.append(chunk_text)
        
        context = "\n\n".join(context_parts)
        
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
