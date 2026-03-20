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


# ---------------------------------------------------------------------------
# Fallback: parse native tool-call formats into Python code
# ---------------------------------------------------------------------------

def extract_tool_call_as_python(text: str, known_tools: set[str] | None = None) -> str:
    """Parse common LLM tool-call formats and convert to executable Python.

    Handles:
      1. XML:  <tool_call> <function=name> <parameter=k> v </parameter> ... </function> </tool_call>
      2. JSON: {"action": "name", "parameters": {...}}  or  {"name": "...", "arguments": {...}}
      3. Bare function-style without code fence: knowledge_base(action="search", ...)

    Returns a Python snippet (e.g. ``result = name(...); print(result)``)
    or empty string if no tool call was detected.
    If a tool call is detected but the tool is not in *known_tools*, returns
    a snippet that prints an error guiding the model to use available tools.
    """
    if not text or not text.strip():
        return ""

    stripped = text.strip()

    _tools = known_tools or {
        "knowledge_base", "terminal", "read_file", "write_file", "delete_file",
        "download_file", "str_replace", "grep", "glob_search",
        "web_search", "web_research", "web_advanced", "web_fetch", "todo_write", "screenops",
    }

    def _unavailable_tool_snippet(name: str) -> str:
        available = ", ".join(sorted(_tools))
        return f'print("Error: tool \'{name}\' is not available. Available tools: {available}. Please use one of these instead.")'

    # ── 1. XML  <tool_call> ... </tool_call>  ────────────────────────
    xml_match = re.search(
        r"<tool_call>\s*<function=(\w+)>(.*?)</function>\s*</tool_call>",
        stripped,
        re.DOTALL,
    )
    if xml_match:
        func_name = xml_match.group(1)
        if func_name not in _tools:
            return _unavailable_tool_snippet(func_name)
        params_block = xml_match.group(2)
        params: dict[str, str] = {}
        for pm in re.finditer(
            r"<parameter=(\w+)>\s*(.*?)\s*</parameter>", params_block, re.DOTALL
        ):
            params[pm.group(1)] = pm.group(2).strip()
        return _build_python_call(func_name, params)

    # ── 2. JSON object  ──────────────────────────────────────────────
    json_match = re.search(r"\{[\s\S]*\}", stripped)
    if json_match:
        import json as _json
        try:
            obj = _json.loads(json_match.group(0))
            if isinstance(obj, dict):
                func_name = obj.get("action") or obj.get("name") or obj.get("function") or ""
                params = obj.get("parameters") or obj.get("arguments") or obj.get("params") or {}
                if func_name and isinstance(params, dict):
                    if func_name not in _tools:
                        return _unavailable_tool_snippet(func_name)
                    return _build_python_call(func_name, params)
        except (_json.JSONDecodeError, TypeError):
            pass

    # ── 3. Bare function call without code fence  ────────────────────
    bare_match = re.search(r"^(\w+)\s*\(.*\)\s*$", stripped, re.MULTILINE)
    if bare_match:
        func_name = bare_match.group(1)
        if func_name not in _tools:
            return _unavailable_tool_snippet(func_name)

    for tool_name in _tools:
        pattern = rf"^{re.escape(tool_name)}\s*\(.*\)\s*$"
        m = re.search(pattern, stripped, re.MULTILINE)
        if m:
            call = m.group(0).strip()
            return f"result = {call}\nprint(result)"

    return ""


def _build_python_call(func_name: str, params: dict) -> str:
    """Turn a function name and params dict into ``result = fn(...); print(result)``."""
    parts: list[str] = []
    for k, v in params.items():
        parts.append(f"{k}={_py_repr(v)}")
    args_str = ", ".join(parts)
    return f"result = {func_name}({args_str})\nprint(result)"


def _py_repr(value) -> str:
    """Best-effort Python repr for a value extracted from JSON/XML."""
    if isinstance(value, str):
        # Value might be a JSON-encoded list/dict string like '["a","b"]'
        if (value.startswith("[") and value.endswith("]")) or (
            value.startswith("{") and value.endswith("}")
        ):
            import json as _json
            try:
                parsed = _json.loads(value)
                return repr(parsed)
            except Exception:
                pass
        return repr(value)
    return repr(value)


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
