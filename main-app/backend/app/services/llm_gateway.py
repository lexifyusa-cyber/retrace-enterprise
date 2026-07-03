"""Central LLM and embedding gateway for configured model providers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings


def _clean_url(value: str | None) -> str:
    return (value or "").strip().rstrip("/")


def _clean_key(value: str | None) -> str:
    return (value or "").strip()


@dataclass(frozen=True)
class LLMGatewayConfig:
    provider: str
    chat_base_url: str
    chat_api_key: str
    chat_model: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    embedding_collection: str
    embedding_debug_log: bool


class LLMGateway:
    """Wrapper around configured chat and embedding endpoints.

    ReTrace stores chat, ScreenOps, and embedding settings separately. This
    gateway is intentionally scoped to chat + embeddings so training, RAG, and
    settings tests do not each rebuild slightly different OpenAI-compatible
    client logic.
    """

    def __init__(self, config: LLMGatewayConfig):
        self.config = config

    @classmethod
    async def from_db(cls, session: AsyncSession) -> "LLMGateway":
        from app.api.settings import get_active_llm_settings

        return cls.from_settings(await get_active_llm_settings(session))

    @classmethod
    def from_settings(cls, llm: dict[str, Any]) -> "LLMGateway":
        provider = (llm.get("provider") or "openai").strip()
        chat_base_url = _clean_url(llm.get("api_url"))
        chat_api_key = _clean_key(
            llm.get("api_key")
            or app_settings.OPENAI_API_KEY
            or os.getenv("OPENAI_API_KEY")
        )

        embedding_base_url = _clean_url(
            llm.get("embedding_base_url")
            or app_settings.TEAMKNOWPARSE_EMBEDDING_BASE_URL
        )
        embedding_api_key = _clean_key(
            llm.get("embedding_api_key")
            or app_settings.EMBEDDING_API_KEY
            or os.getenv("EMBEDDING_API_KEY")
        )
        if embedding_base_url and not embedding_api_key:
            # Many local OpenAI-compatible embedding servers only require the
            # header to be present, not a real secret.
            embedding_api_key = "local-vllm"

        return cls(
            LLMGatewayConfig(
                provider=provider,
                chat_base_url=chat_base_url,
                chat_api_key=chat_api_key,
                chat_model=(llm.get("model_name") or app_settings.REASONING_MODEL).strip(),
                embedding_base_url=embedding_base_url,
                embedding_api_key=embedding_api_key,
                embedding_model=(
                    llm.get("embedding_model")
                    or app_settings.TEAMKNOWPARSE_EMBEDDING_MODEL
                ).strip(),
                embedding_collection=(
                    llm.get("embedding_chroma_collection")
                    or app_settings.TEAMKNOWPARSE_CHROMA_COLLECTION
                ).strip(),
                embedding_debug_log=bool(
                    llm.get("embedding_debug_log")
                    if llm.get("embedding_debug_log") is not None
                    else app_settings.TEAMKNOWPARSE_EMBEDDING_DEBUG_LOG
                ),
            )
        )

    def chat_client(self) -> AsyncOpenAI:
        if not self.config.chat_api_key:
            raise RuntimeError("LLM API key is required for chat completions")
        kwargs: dict[str, Any] = {"api_key": self.config.chat_api_key}
        if self.config.chat_base_url:
            kwargs["base_url"] = self.config.chat_base_url
        return AsyncOpenAI(**kwargs)

    def embedding_client(self) -> AsyncOpenAI:
        if not self.config.embedding_api_key:
            raise RuntimeError("Embedding API key is required for vector embeddings")
        kwargs: dict[str, Any] = {"api_key": self.config.embedding_api_key}
        if self.config.embedding_base_url:
            kwargs["base_url"] = self.config.embedding_base_url
        return AsyncOpenAI(**kwargs)

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        token_limit: int = 1024,
        temperature: float | None = None,
        **kwargs: Any,
    ):
        client = self.chat_client()
        request: dict[str, Any] = {
            "model": model or self.config.chat_model,
            "messages": messages,
            "max_completion_tokens": token_limit,
            **kwargs,
        }
        if temperature is not None:
            request["temperature"] = temperature
        try:
            return await client.chat.completions.create(**request)
        except Exception as exc:
            msg = str(exc)
            if "max_completion_tokens" in msg and "Unsupported parameter" in msg:
                request["max_tokens"] = request.pop("max_completion_tokens")
                return await client.chat.completions.create(**request)
            raise

    async def embed_texts(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        response = await self.embedding_client().embeddings.create(
            model=model or self.config.embedding_model,
            input=texts,
        )
        return [item.embedding for item in response.data]

    async def test_chat(self) -> dict[str, Any]:
        response = await self.chat_completion(
            [{"role": "user", "content": "Say OK"}],
            token_limit=5,
        )
        return {
            "success": True,
            "model": response.model,
            "response": response.choices[0].message.content,
        }

    async def test_embeddings(self) -> dict[str, Any]:
        vectors = await self.embed_texts(["test embedding"])
        return {
            "success": True,
            "model": self.config.embedding_model,
            "dimensions": len(vectors[0]) if vectors else 0,
        }
