"""
WebFetch tool — fetch URL content and return as clean readable text.

Uses ``httpx`` for HTTP requests and ``trafilatura`` for content
extraction.  Falls back to ``beautifulsoup4`` + basic text extraction
when trafilatura is not installed or extraction fails.
"""

import structlog

logger = structlog.get_logger()

MAX_RESPONSE_SIZE = 5_000_000  # 5 MB raw HTML
MAX_TEXT_SIZE = 200_000  # 200 KB extracted text

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def web_fetch(url: str) -> str:
    """Fetch a URL and return its content as clean readable text.

    Parameters
    ----------
    url : str
        The fully-formed URL to fetch.

    Returns
    -------
    str
        Extracted text content, or an error message.
    """
    if not url or not url.strip():
        return "Error: empty URL"

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        import httpx
    except ImportError:
        return "Error: httpx package not installed. Run: pip install httpx"

    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=30.0,
            headers=_HEADERS,
        ) as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Error: HTTP {exc.response.status_code} fetching {url}"
    except httpx.ConnectError:
        return f"Error: could not connect to {url}"
    except httpx.TimeoutException:
        return f"Error: request timed out fetching {url}"
    except Exception as exc:
        return f"Error fetching URL: {exc}"

    content_type = response.headers.get("content-type", "")
    raw = response.text

    if len(raw) > MAX_RESPONSE_SIZE:
        raw = raw[:MAX_RESPONSE_SIZE]

    if "application/json" in content_type:
        if len(raw) > MAX_TEXT_SIZE:
            raw = raw[:MAX_TEXT_SIZE] + "\n[truncated]"
        return f"JSON response from {url}:\n\n{raw}"

    if "text/plain" in content_type:
        if len(raw) > MAX_TEXT_SIZE:
            raw = raw[:MAX_TEXT_SIZE] + "\n[truncated]"
        return f"Text from {url}:\n\n{raw}"

    text = _extract_with_trafilatura(raw, url)
    if not text:
        text = _extract_with_bs4(raw)
    if not text:
        text = _extract_basic(raw)

    if not text or not text.strip():
        return f"Could not extract readable content from {url}"

    if len(text) > MAX_TEXT_SIZE:
        text = text[:MAX_TEXT_SIZE] + "\n\n[content truncated]"

    return f"Content from {url}:\n\n{text}"


def _extract_with_trafilatura(html: str, url: str) -> str:
    try:
        import trafilatura
        result = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            output_format="txt",
            favor_recall=True,
        )
        return result or ""
    except ImportError:
        return ""
    except Exception:
        return ""


def _extract_with_bs4(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
        return "\n".join(lines)
    except ImportError:
        return ""
    except Exception:
        return ""


def _extract_basic(html: str) -> str:
    import re
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
