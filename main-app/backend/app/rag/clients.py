"""
Concrete implementations of EmbeddingClient and VisionLLMClient using OpenAI.
"""

import base64
from pathlib import Path
from typing import Optional

import structlog
from openai import AsyncOpenAI

from app.rag.interfaces import EmbeddingClient, VisionLLMClient

logger = structlog.get_logger()

MEDIA_TYPE_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}


class OpenAIEmbeddingClient(EmbeddingClient):
    """Generate embeddings via an OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        base_url: Optional[str] = None,
    ):
        kw: dict = {"api_key": api_key}
        if base_url:
            kw["base_url"] = base_url
        self._client = AsyncOpenAI(**kw)
        self._model = model

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.embeddings.create(
            model=self._model,
            input=texts,
        )
        # The API returns items in the same order as input
        return [item.embedding for item in response.data]


def get_embedding_client(llm_settings: dict) -> "OpenAIEmbeddingClient":
    """Build an OpenAIEmbeddingClient from LLM settings dict."""
    api_key = llm_settings.get("api_key", "")
    emb_key = llm_settings.get("embeddings_api_key") or api_key
    if llm_settings.get("embeddings_api_key"):
        emb_base_url = llm_settings.get("embeddings_api_url") or None
    else:
        emb_base_url = llm_settings.get("embeddings_api_url") or llm_settings.get("api_url") or None
    return OpenAIEmbeddingClient(
        api_key=emb_key,
        base_url=emb_base_url,
        model=llm_settings.get("embeddings_model") or "text-embedding-3-small",
    )


class OpenAIVisionClient(VisionLLMClient):
    """Describe images via an OpenAI-compatible vision model."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: Optional[str] = None,
    ):
        kw: dict = {"api_key": api_key}
        if base_url:
            kw["base_url"] = base_url
        self._client = AsyncOpenAI(**kw)
        self._model = model

    async def describe_image(
        self, image_path: str, prompt: Optional[str] = None
    ) -> str:
        data = Path(image_path).read_bytes()
        b64 = base64.b64encode(data).decode("utf-8")
        ext = Path(image_path).suffix.lower()
        media = MEDIA_TYPE_MAP.get(ext, "image/png")

        default_prompt = (
            "Describe this diagram in detail. "
            "List all components, connections, labels, and data flows."
        )

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt or default_prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media};base64,{b64}",
                            },
                        },
                    ],
                }
            ],
            max_tokens=1000,
        )
        return response.choices[0].message.content or ""
