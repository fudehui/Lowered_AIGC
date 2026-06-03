from __future__ import annotations

from pathlib import Path
from typing import Any

from .langgraph_agent import run_langgraph_agent
from .models import AgentSummary
from ..rewrite.adapters import PromptSkillCandidate, PromptSkillSpec


def run_agent(
    docx_path: str | Path,
    report_path: str | Path,
    output_docx: str | Path,
    report_json: str | Path,
    *,
    llm_options: dict[str, Any],
    prompt_skill: PromptSkillSpec | None = None,
    skill_candidates: list[PromptSkillCandidate] | None = None,
    quality_options: dict[str, Any] | None = None,
    review_options: dict[str, Any] | None = None,
    max_spans: int = 80,
    min_align_score: float = 0.58,
    max_retries: int = 2,
) -> AgentSummary:
    return run_langgraph_agent(
        docx_path=docx_path,
        report_path=report_path,
        output_docx=output_docx,
        report_json=report_json,
        llm_options=llm_options,
        prompt_skill=prompt_skill,
        skill_candidates=skill_candidates,
        quality_options=quality_options,
        review_options=review_options,
        max_spans=max_spans,
        min_align_score=min_align_score,
        max_retries=max_retries,
    )
