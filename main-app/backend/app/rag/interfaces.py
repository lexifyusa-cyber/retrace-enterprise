"""
Abstract interfaces for external service clients.

Implement these to plug in your own embedding / vision providers.
"""

from abc import ABC, abstractmethod
from typing import Optional


class EmbeddingClient(ABC):
    """Generate vector embeddings for text."""

    @abstractmethod
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""
        ...


class VisionLLMClient(ABC):
    """Describe images using a vision-capable LLM."""

    @abstractmethod
    async def describe_image(
        self, image_path: str, prompt: Optional[str] = None
    ) -> str:
        """Return a textual description of the image at *image_path*."""
        ...
