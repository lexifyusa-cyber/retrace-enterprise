"""
Token counting utilities for AutoBrowser content analysis.

Provides functions to estimate token consumption for different content
extraction approaches (text extraction, screenshots, multi-tile captures).
"""

from __future__ import annotations


def count_text_tokens(text: str) -> int:
    """Count tokens using tiktoken (cl100k_base).

    Uses the same encoding as the RAG system for consistency.
    Falls back to character-based estimation if tiktoken is unavailable.

    Args:
        text: The text to count tokens for

    Returns:
        Estimated token count
    """
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # Fallback: rough estimate based on character count
        # Typical English text is ~3-4 characters per token
        return max(1, len(text) // 3)


def estimate_vision_tokens(num_screenshots: int = 1) -> int:
    """Estimate vision tokens for screenshot(s).

    Simple approximation: ~500 tokens per 1280×900 screenshot.
    This is a conservative estimate across different LLM providers.

    Args:
        num_screenshots: Number of screenshots to estimate for

    Returns:
        Estimated total vision tokens
    """
    return 500 * num_screenshots


def calculate_screenshot_count(
    page_height: int,
    viewport_height: int = 900,
) -> int:
    """Calculate how many full-viewport screenshots cover the entire page.

    Args:
        page_height: Total page height in pixels (from document.body.scrollHeight)
        viewport_height: Height of each screenshot viewport (default: 900px)

    Returns:
        Number of screenshots needed to cover the full page
    """
    if page_height <= 0:
        return 1
    # Ceiling division: (height + viewport - 1) // viewport
    return (page_height + viewport_height - 1) // viewport_height
