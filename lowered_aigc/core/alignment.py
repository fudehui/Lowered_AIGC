from __future__ import annotations

from .models import Alignment, DocxParagraph, ReportSpan
from ..utils.text import partial_ratio


def align_spans_to_docx(
    spans: list[ReportSpan],
    paragraphs: list[DocxParagraph],
    min_score: float = 0.58,
) -> list[Alignment]:
    alignments: list[Alignment] = []
    used: set[str] = set()

    candidates = [p for p in paragraphs if len(p.text.strip()) >= 24]
    for span in spans:
        best: tuple[float, DocxParagraph | None] = (0.0, None)
        for paragraph in candidates:
            score = partial_ratio(span.text, paragraph.text)
            if score > best[0]:
                best = (score, paragraph)
        score, paragraph = best
        if paragraph is None or score < min_score:
            continue
        if paragraph.key in used:
            continue
        used.add(paragraph.key)
        alignments.append(
            Alignment(
                span=span,
                paragraph=paragraph,
                score=score,
                reason="partial_ratio",
            )
        )
    return alignments
