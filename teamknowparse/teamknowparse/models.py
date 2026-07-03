from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


PageType = Literal[
    "DIGITAL_PURE_TEXT",
    "FORM",
    "TABLE",
    "SCANNED",
    "VISUAL",
    "MIXED",
    "FAILED",
]


class ListItem(BaseModel):
    text: str
    level: int = 0


class KeyValuePair(BaseModel):
    key: str
    value: str | None = None


class Element(BaseModel):
    type: str
    text: str | None = None
    level: int | None = None
    ordered: bool | None = None
    items: list[ListItem] | None = None
    title: str | None = None
    pairs: list[KeyValuePair] | None = None
    table_description: str | None = None
    markdown: str | None = None
    kind: str | None = None
    summary: str | None = None
    image_description: str | None = None

    @field_validator("type")
    @classmethod
    def allowed_type(cls, value: str) -> str:
        allowed = {
            "title",
            "section_header",
            "paragraph",
            "list",
            "table",
            "image",
            "key_value_pairs",
        }
        if value not in allowed:
            raise ValueError(f"Unsupported element type: {value}")
        return value


class PageJson(BaseModel):
    page_number: int
    page_type: PageType
    elements: list[Element] = Field(default_factory=list)
    rag_text: str


class FileJson(BaseModel):
    filename: str
    file_path: str
    page_count: int
    tags: list[str] = Field(default_factory=list)
    pages: list[PageJson] = Field(default_factory=list)


class FolderFile(BaseModel):
    filename: str
    file_path: str
    page_count: int
    tags: list[str] = Field(default_factory=list)
    file_description: str


class FolderJson(BaseModel):
    folder_name: str
    folder_path: str
    folder_description: str
    total_file_count: int
    files: list[FolderFile]


class PageAudit(BaseModel):
    filename: str
    page_number: int
    route: str
    page_type: PageType
    status: Literal["passed", "failed", "needs_review", "skipped"]
    latency_ms: int
    retry_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    internal: dict[str, Any] = Field(default_factory=dict)

