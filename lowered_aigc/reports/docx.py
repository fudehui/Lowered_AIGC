from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from ..core.models import ReportSpan
from ..utils.text import clean_text
from .pdf import _dedupe_spans, _page_text_to_spans


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"


RISK_COLOR_MAP = {
    "F12828": "high",
    "F39800": "medium",
    "9D91E9": "low",
}

IGNORE_COLORS = {"000000", "1F1F1F", "B0B0B0", "0000FF", "AUTO"}


def extract_docx_report_spans(report_path: str | Path, max_spans: int = 200) -> list[ReportSpan]:
    """Extract risky spans from a DOCX AIGC report.

    PaperPass-style DOCX reports often contain the submitted paper with risky
    fragments marked by font color instead of PDF highlight annotations. This
    parser reads the OOXML directly so conda base can run it without
    python-docx.
    """

    report_path = Path(report_path)
    with zipfile.ZipFile(report_path) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)

    color_spans = _extract_colored_spans(root)
    if color_spans:
        return _dedupe_spans(color_spans)[:max_spans]

    text_spans: list[ReportSpan] = []
    for idx, paragraph in enumerate(root.iter(W + "p"), start=1):
        text = _paragraph_text(paragraph)
        if not text:
            continue
        text_spans.extend(_page_text_to_spans(text, page=idx))
    for span in text_spans:
        span.source = "docx_text_fallback"
    return _dedupe_spans(text_spans)[:max_spans]


def _extract_colored_spans(root: ET.Element) -> list[ReportSpan]:
    spans: list[ReportSpan] = []
    for para_index, paragraph in enumerate(root.iter(W + "p"), start=1):
        fragments: list[tuple[str, str]] = []
        current_risk: str | None = None
        buffer: list[str] = []

        for run in paragraph.iter(W + "r"):
            text = _run_text(run)
            if not text:
                continue
            color = _run_color(run)
            risk = _risk_from_color(color)
            if risk is None:
                if buffer and current_risk:
                    fragments.append((current_risk, "".join(buffer)))
                buffer = []
                current_risk = None
                continue
            if current_risk and risk != current_risk:
                fragments.append((current_risk, "".join(buffer)))
                buffer = []
            current_risk = risk
            buffer.append(text)

        if buffer and current_risk:
            fragments.append((current_risk, "".join(buffer)))

        for risk, text in fragments:
            text = clean_text(text)
            if len(text) < 24:
                continue
            spans.append(
                ReportSpan(
                    text=text,
                    page=None,
                    source="docx_color_text",
                    risk=risk,
                    metadata={"paragraph_index": para_index},
                )
            )
    return spans


def _paragraph_text(paragraph: ET.Element) -> str:
    return clean_text("".join(t.text or "" for t in paragraph.iter(W + "t")))


def _run_text(run: ET.Element) -> str:
    return "".join(t.text or "" for t in run.iter(W + "t"))


def _run_color(run: ET.Element) -> str | None:
    rpr = run.find(W + "rPr")
    if rpr is None:
        return None
    color = rpr.find(W + "color")
    if color is None:
        return None
    value = color.attrib.get(W + "val")
    return value.upper() if value else None


def _risk_from_color(color: str | None) -> str | None:
    if not color or color in IGNORE_COLORS:
        return None
    return RISK_COLOR_MAP.get(color, "unknown")
