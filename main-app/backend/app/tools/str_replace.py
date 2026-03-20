"""
StrReplace tool — precise text replacement in files.

Performs exact string matching and replacement.  When ``replace_all`` is
False (default), the edit fails if the *old_string* is not unique in the
file — the caller must provide more surrounding context to disambiguate.
"""

import os
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()


def str_replace(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Replace exact text in a file.

    Parameters
    ----------
    file_path : str
        Path to the file to edit (relative or absolute).
    old_string : str
        The exact text to find.  Must differ from *new_string*.
    new_string : str
        Replacement text.
    replace_all : bool
        If True, replace every occurrence.  If False (default), require
        exactly one occurrence or fail with an error.

    Returns
    -------
    str
        Success message with a short diff preview, or an error string.
    """
    if old_string == new_string:
        return "Error: old_string and new_string are identical"
    if not old_string:
        return "Error: old_string is empty"

    from app.tools.terminal import _cwd
    file_path = os.path.expanduser(file_path)
    p = Path(file_path)
    if not p.is_absolute():
        p = Path(_cwd) / p
    p = p.resolve()

    if not p.exists():
        return f"Error: file not found — {p}"
    if not p.is_file():
        return f"Error: not a file — {p}"

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error reading file: {exc}"

    count = content.count(old_string)
    if count == 0:
        snippet = old_string[:80].replace("\n", "\\n")
        return f'Error: string not found in {p.name}. Searched for: "{snippet}"'

    if count > 1 and not replace_all:
        return (
            f"Error: found {count} occurrences of the string in {p.name}. "
            "Provide more surrounding context to make it unique, or set replace_all=True."
        )

    if replace_all:
        new_content = content.replace(old_string, new_string)
        replacements = count
    else:
        new_content = content.replace(old_string, new_string, 1)
        replacements = 1

    try:
        p.write_text(new_content, encoding="utf-8")
    except Exception as exc:
        return f"Error writing file: {exc}"

    old_preview = old_string.strip().split("\n")
    new_preview = new_string.strip().split("\n")
    diff_lines = []
    for line in old_preview[:3]:
        diff_lines.append(f"- {line}")
    if len(old_preview) > 3:
        diff_lines.append(f"  ... ({len(old_preview) - 3} more lines)")
    for line in new_preview[:3]:
        diff_lines.append(f"+ {line}")
    if len(new_preview) > 3:
        diff_lines.append(f"  ... ({len(new_preview) - 3} more lines)")
    diff_preview = "\n".join(diff_lines)

    return (
        f"Successfully replaced {replacements} occurrence(s) in {p.name}\n\n"
        f"{diff_preview}"
    )
