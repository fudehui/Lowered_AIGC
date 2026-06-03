from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ReportSpan:
    text: str
    page: int | None = None
    source: str = "pdf_text"
    risk: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DocxParagraph:
    part_name: str
    index: int
    text: str

    @property
    def key(self) -> str:
        return f"{self.part_name}#{self.index}"


@dataclass(slots=True)
class Alignment:
    span: ReportSpan
    paragraph: DocxParagraph
    score: float
    reason: str = ""


@dataclass(slots=True)
class RewriteAttempt:
    original: str
    rewritten: str
    ok: bool
    issues: list[str] = field(default_factory=list)
    attempts: int = 1


@dataclass(slots=True)
class AgentSummary:
    input_docx: str
    input_report: str
    output_docx: str
    report_json: str
    spans_found: int
    alignments_found: int
    paragraphs_rewritten: int
    skipped: list[dict[str, Any]] = field(default_factory=list)
