from __future__ import annotations

from pathlib import Path

from ..core.models import ReportSpan
from .docx import extract_docx_report_spans
from .pdf import extract_report_spans as extract_pdf_report_spans


def extract_report_spans(report_path: str | Path, max_spans: int = 200) -> list[ReportSpan]:
    report_path = Path(report_path)
    suffix = report_path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_report_spans(report_path, max_spans=max_spans)
    if suffix == ".docx":
        return extract_docx_report_spans(report_path, max_spans=max_spans)
    raise ValueError(f"Unsupported report file type: {report_path.suffix}. Use .pdf or .docx")
