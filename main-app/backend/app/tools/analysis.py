"""
Content extraction analysis for AutoBrowser.

Analyzes page content and compares token costs for different extraction
approaches (read_page, screenshot, multi_tile). Returns data for the LLM
to make intelligent decisions.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.tools.token_analyzer import (
    calculate_screenshot_count,
    count_text_tokens,
    estimate_vision_tokens,
)


@dataclass
class ExtractionOption:
    """Represents one content extraction approach and its token cost."""

    approach: str  # "read_page", "screenshot", or "multi_tile"
    total_tokens: int  # Estimated total tokens for this approach
    num_screenshots: int  # 0 for read_page, 1+ for screenshot approaches
    description: str  # Human-readable description


def analyze_extraction_options(
    text_content: str,
    page_height: int,
) -> list[ExtractionOption]:
    """Generate token cost analysis for different content extraction approaches.

    Analyzes the page and returns options for read_page, single screenshot,
    and multi-tile screenshot approaches. The LLM will review these options
    and decide which method to use based on token efficiency and context needs.

    Args:
        text_content: Full extracted page text from page.inner_text("body")
        page_height: Page height in pixels from document.body.scrollHeight

    Returns:
        List of ExtractionOption objects, in order: read_page, screenshot, multi_tile
    """
    options = []

    # Option 1: read_page — extract all text at once
    text_tokens = count_text_tokens(text_content)
    options.append(
        ExtractionOption(
            approach="read_page",
            total_tokens=text_tokens,
            num_screenshots=0,
            description=f"Extract all page text ({len(text_content)} chars → {text_tokens} tokens)",
        )
    )

    # Option 2: screenshot — single viewport screenshot
    options.append(
        ExtractionOption(
            approach="screenshot",
            total_tokens=500,
            num_screenshots=1,
            description="Capture single screenshot (500 tokens)",
        )
    )

    # Option 3: multi_tile — multiple screenshots for full page coverage
    # Only add if page is taller than one viewport
    screenshot_count = calculate_screenshot_count(page_height)
    if screenshot_count > 1:
        vision_tokens = estimate_vision_tokens(screenshot_count)
        options.append(
            ExtractionOption(
                approach="multi_tile",
                total_tokens=vision_tokens,
                num_screenshots=screenshot_count,
                description=f"Capture {screenshot_count} screenshots for full page coverage ({vision_tokens} tokens)",
            )
        )

    return options
