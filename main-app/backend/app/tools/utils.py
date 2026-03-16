"""
CodeAct utilities for the ReTrace agent.

Ported from IQWorksAtlas langgraph_codeact/utils.py — code-block extraction,
language detection, and validation helpers used by the LangGraph agent loop.
"""

import ast
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Language classification
# ---------------------------------------------------------------------------

EXECUTABLE_LANGUAGES = {
    "python", "py", "bash", "sh", "shell", "zsh", "fish",
    "javascript", "js", "node", "typescript", "ts",
    "ruby", "rb", "perl", "pl", "php", "r", "lua",
    "go", "rust", "rs", "java", "c", "cpp", "c++", "csharp", "cs",
    "sql", "powershell", "ps1", "cmd", "bat",
}

CONTENT_LANGUAGES = {
    "markdown", "md", "text", "txt", "plain", "plaintext",
    "json", "yaml", "yml", "xml", "html", "css",
    "csv", "toml", "ini", "conf", "config", "log",
}

BACKTICK_PATTERN = r"```(.*?)```"


def is_content_language(lang: str) -> bool:
    return (lang.strip().lower() in CONTENT_LANGUAGES) if lang else False


# ---------------------------------------------------------------------------
# Python validation
# ---------------------------------------------------------------------------

def _is_valid_python_code(code: str) -> bool:
    """Return True if *code* parses as valid Python."""
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _looks_like_python_code(code: str) -> bool:
    """Heuristic: does *code* look like Python rather than bash/JS/etc.?"""
    indicators = ["print(", "import ", "def ", "class ", "for ", "if ", "return ",
                   "= ", "==", "True", "False", "None", "async ", "await "]
    anti = ["#!/bin/", "echo ", "export ", "sudo ", "apt ", "npm ", "const ", "let ", "var "]
    score = sum(1 for i in indicators if i in code) - sum(2 for a in anti if a in code)
    return score > 0


# ---------------------------------------------------------------------------
# Code extraction
# ---------------------------------------------------------------------------

def extract_python_code(text: str) -> str:
    """Extract the first executable Python code block from LLM output.

    Priority:
    1. ```python ... ``` block
    2. Generic ``` ... ``` block that validates as Python
    Returns empty string if nothing found.
    """
    if not text or "```" not in text:
        return ""

    # 1. Explicit python block
    m = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if m:
        code = m.group(1).strip()
        if _is_valid_python_code(code):
            return code

    # 2. Generic code block that parses as Python
    content_patterns = [
        r"```markdown", r"```md", r"```text", r"```txt", r"```plain",
        r"```json", r"```yaml", r"```yml", r"```xml", r"```html", r"```css",
        r"```csv", r"```toml", r"```ini", r"```conf", r"```config", r"```log",
    ]
    has_content_blocks = any(re.search(p, text, re.IGNORECASE) for p in content_patterns)
    if not has_content_blocks:
        m2 = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
        if m2:
            code = m2.group(1).strip()
            if _is_valid_python_code(code) and _looks_like_python_code(code):
                return code

    return ""


def strip_text_from_code_response(content: str) -> str:
    """Strip explanatory text from a response that contains code blocks.

    Returns only the valid code blocks so the LLM doesn't echo its own
    prose into future code (which causes SyntaxError loops).
    """
    if "```" not in content:
        return content

    pattern = r"(?:^|\n)(```[a-zA-Z0-9_]+\n.*?```(?:\n|$))"
    matches = list(re.finditer(pattern, content, re.DOTALL))

    if not matches:
        # Fallback: any block that looks like code
        all_pattern = r"(?:^|\n)(```.*?```(?:\n|$))"
        all_matches = list(re.finditer(all_pattern, content, re.DOTALL))
        code_indicators = ["=", "(", ")", "[", "]", "{", "}", "def ", "import ", "print("]
        text_indicators = ["I have", "I need", "I should", "Now,", "Then,", "After this"]
        for match in all_matches:
            block = match.group(1)
            block_content = block[3:-3]
            first_nl = block_content.find("\n")
            if first_nl > 0 and " " not in block_content[:first_nl]:
                block_content = block_content[first_nl + 1:]
            has_code = any(i in block_content for i in code_indicators)
            has_text = any(i in block_content for i in text_indicators)
            if has_code and not has_text:
                matches.append(match)

    if not matches:
        return content

    blocks = [m.group(1).strip("\n") for m in matches if m.group(1).strip("\n")]
    return "\n\n".join(blocks) if blocks else content
