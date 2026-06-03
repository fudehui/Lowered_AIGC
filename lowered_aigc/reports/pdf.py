from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from ..core.models import ReportSpan
from ..utils.text import clean_text


class PdfReportError(RuntimeError):
    pass


REPORT_NOISE = (
    "PaperPass",
    "AIGC检测报告",
    "检测报告",
    "检测结果",
    "相似度分布",
    "报告编号",
    "提交时间",
    "查询真伪",
    "片段总数",
    "全文字数",
)


def _load_pypdf():
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - depends on local env
        raise PdfReportError(
            "缺少 pypdf，无法读取 PDF。请安装 pypdf，或使用带 pypdf 的运行环境。"
        ) from exc
    return PdfReader


def extract_report_spans(pdf_path: str | Path, max_spans: int = 200) -> list[ReportSpan]:
    """Extract the queue of risky text spans from a report PDF.

    Priority:
    1. Real PDF highlight annotations with comment text.
    2. Text fallback for PaperPass-like reports where risk is represented by page text
       and colors instead of annotation objects.
    """

    pdf_path = Path(pdf_path)
    PdfReader = _load_pypdf()
    try:
        reader = PdfReader(str(pdf_path))
        if reader.is_encrypted:
            reader.decrypt("")
    except Exception as exc:
        raise PdfReportError(
            f"无法打开 PDF：{pdf_path.name}。若报告是 AES 加密 PDF，请在当前 Python 环境安装 cryptography。"
        ) from exc

    try:
        spans = _extract_annotation_text(reader)
        if spans:
            return _dedupe_spans(spans)[:max_spans]

        text_spans = _extract_textual_risk_spans(reader)
        return _dedupe_spans(text_spans)[:max_spans]
    except Exception as exc:
        if "cryptography" in str(exc):
            raise PdfReportError(
                "该 PDF 使用 AES 加密，当前 Python 环境缺少 cryptography，pypdf 无法解密。"
            ) from exc
        raise


def _extract_annotation_text(reader) -> list[ReportSpan]:
    spans: list[ReportSpan] = []
    for page_index, page in enumerate(reader.pages, start=1):
        annots = page.get("/Annots") or []
        for ref in annots:
            try:
                obj = ref.get_object()
            except Exception:
                continue
            subtype = str(obj.get("/Subtype", ""))
            if subtype != "/Highlight":
                continue
            contents = clean_text(str(obj.get("/Contents", "")))
            if not contents:
                continue
            spans.append(
                ReportSpan(
                    text=contents,
                    page=page_index,
                    source="pdf_highlight_annotation",
                    risk="highlight",
                    metadata={"subtype": subtype},
                )
            )
    return spans


def _extract_textual_risk_spans(reader) -> list[ReportSpan]:
    spans: list[ReportSpan] = []
    for page_index, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception:
            continue
        spans.extend(_page_text_to_spans(page_text, page_index))
    return spans


def _page_text_to_spans(page_text: str, page: int) -> Iterable[ReportSpan]:
    lines = [clean_text(line) for line in page_text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []

    merged: list[str] = []
    buffer = ""
    for line in lines:
        if _is_noise(line):
            if buffer:
                merged.append(buffer)
                buffer = ""
            continue
        if _looks_like_segment_boundary(line):
            if buffer:
                merged.append(buffer)
            buffer = line
        else:
            if not buffer:
                buffer = line
            elif len(buffer) < 500:
                buffer += " " + line
            else:
                merged.append(buffer)
                buffer = line
    if buffer:
        merged.append(buffer)

    spans: list[ReportSpan] = []
    for item in merged:
        risk = _infer_risk(item)
        cleaned = _strip_report_metrics(item)
        if len(cleaned) < 24:
            continue
        if _is_noise(cleaned):
            continue
        spans.append(
            ReportSpan(
                text=cleaned,
                page=page,
                source="pdf_text_fallback",
                risk=risk,
                metadata={"raw": item[:600]},
            )
        )
    return spans


def _is_noise(line: str) -> bool:
    if any(token in line for token in REPORT_NOISE):
        return True
    if re.fullmatch(r"[\d\s./:%~\-]+", line):
        return True
    return False


def _looks_like_segment_boundary(line: str) -> bool:
    if re.search(r"(AIGC|疑似|相似|风险|高|中|低).{0,12}(\d{1,3}(?:\.\d+)?%)", line):
        return True
    if re.match(r"^\d{1,3}[、.．]\s*", line):
        return True
    return len(line) > 80


def _infer_risk(text: str) -> str | None:
    if re.search(r"(高|红色|70%|8\d%|9\d%|100%)", text):
        return "high"
    if re.search(r"(中|橙色|60%|6\d%)", text):
        return "medium"
    if re.search(r"(低|黄色|50%|5\d%)", text):
        return "low"
    if "AIGC" in text or "疑似" in text:
        return "unknown"
    return None


def _strip_report_metrics(text: str) -> str:
    text = re.sub(r"AIGC[^，。；;]{0,30}?\d{1,3}(?:\.\d+)?%", " ", text)
    text = re.sub(r"(总)?相似度[^，。；;]{0,30}?\d{1,3}(?:\.\d+)?%", " ", text)
    text = re.sub(r"^\d{1,3}[、.．]\s*", "", text)
    return clean_text(text)


def _dedupe_spans(spans: list[ReportSpan]) -> list[ReportSpan]:
    seen: set[str] = set()
    unique: list[ReportSpan] = []
    for span in spans:
        key = re.sub(r"\W+", "", span.text).lower()[:160]
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(span)
    return unique
