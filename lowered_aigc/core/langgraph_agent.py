from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, TypedDict

from .alignment import align_spans_to_docx
from .human_review import (
    HUMAN_REVIEW_MCP_NAME,
    append_review_queue_item,
    approved_text,
    human_review_candidate,
    load_review_decisions,
    review_id_for,
)
from .models import AgentSummary, Alignment, ReportSpan
from .quality import validate_rewrite_with_thresholds
from ..docx.package import DocxPackage
from ..reports.extract import extract_report_spans
from ..rewrite.adapters import (
    LLMRewriteSkill,
    PromptSkillCandidate,
    PromptSkillSpec,
    mask_fragile_text,
    route_capability,
    unmask_text,
)
from ..utils.text import compact_snippet


LOGGER = logging.getLogger(__name__)
RewriteSkillFactory = Callable[[PromptSkillSpec, dict[str, Any]], LLMRewriteSkill]


class AgentState(TypedDict, total=False):
    docx_path: Path
    report_path: Path
    output_docx: Path
    report_json: Path
    llm_options: dict[str, Any]
    prompt_skill: PromptSkillSpec
    skill_candidates: list[PromptSkillCandidate]
    capability_route: dict[str, Any]
    quality_options: dict[str, Any]
    review_options: dict[str, Any]
    max_spans: int
    min_align_score: float
    max_retries: int
    rewrite_skill_factory: RewriteSkillFactory
    spans: list[ReportSpan]
    alignments: list[Alignment]
    skipped: list[dict[str, Any]]
    review_pending: list[dict[str, Any]]
    paragraphs_rewritten: int
    skill_name: str
    summary: AgentSummary


def run_langgraph_agent(
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
    rewrite_skill_factory: RewriteSkillFactory | None = None,
) -> AgentSummary:
    state: AgentState = {
        "docx_path": Path(docx_path),
        "report_path": Path(report_path),
        "output_docx": Path(output_docx),
        "report_json": Path(report_json),
        "llm_options": llm_options,
        "quality_options": quality_options or {},
        "review_options": review_options or {},
        "max_spans": max_spans,
        "min_align_score": min_align_score,
        "max_retries": max_retries,
        "rewrite_skill_factory": rewrite_skill_factory or _default_rewrite_skill_factory,
        "skipped": [],
        "review_pending": [],
        "paragraphs_rewritten": 0,
    }
    if prompt_skill:
        state["prompt_skill"] = prompt_skill
    if skill_candidates:
        state["skill_candidates"] = skill_candidates

    graph = _build_graph()
    LOGGER.info("LangGraph 状态图已构建，开始执行")
    final_state = graph.invoke(state)
    LOGGER.info(
        "流程完成: spans=%s alignments=%s rewritten=%s skipped=%s",
        final_state["summary"].spans_found,
        final_state["summary"].alignments_found,
        final_state["summary"].paragraphs_rewritten,
        len(final_state["summary"].skipped),
    )
    return final_state["summary"]


def _build_graph():
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        END, START, StateGraph = _import_langgraph_from_user_site()

    graph = StateGraph(AgentState)
    graph.add_node("extract_report_spans", _extract_report_spans_node)
    graph.add_node("route_capability", _route_capability_node)
    graph.add_node("align_docx_paragraphs", _align_docx_paragraphs_node)
    graph.add_node("rewrite_docx_paragraphs", _rewrite_docx_paragraphs_node)
    graph.add_node("write_process_report", _write_process_report_node)
    graph.add_edge(START, "extract_report_spans")
    graph.add_edge("extract_report_spans", "route_capability")
    graph.add_edge("route_capability", "align_docx_paragraphs")
    graph.add_edge("align_docx_paragraphs", "rewrite_docx_paragraphs")
    graph.add_edge("rewrite_docx_paragraphs", "write_process_report")
    graph.add_edge("write_process_report", END)
    return graph.compile()


def _import_langgraph_from_user_site():
    import site
    import sys

    user_site = site.getusersitepackages()
    if user_site and user_site not in sys.path:
        sys.path.append(user_site)
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise RuntimeError(
            "LangGraph is required for this agent framework. "
            "Install dependencies with: python -m pip install -r requirements.txt"
        ) from exc
    return END, START, StateGraph


def _extract_report_spans_node(state: AgentState) -> dict[str, Any]:
    LOGGER.info("[1/5] 读取检测报告并抽取风险片段: %s", state["report_path"])
    spans = extract_report_spans(state["report_path"], max_spans=state["max_spans"])
    LOGGER.info("[1/5] 风险片段抽取完成: %s 条", len(spans))
    for index, span in enumerate(spans[:5], 1):
        LOGGER.debug("风险片段 %s: source=%s risk=%s text=%s", index, span.source, span.risk, compact_snippet(span.text))
    if len(spans) > 5:
        LOGGER.debug("其余风险片段省略日志: %s 条", len(spans) - 5)
    return {"spans": spans}


def _route_capability_node(state: AgentState) -> dict[str, Any]:
    if state.get("prompt_skill"):
        LOGGER.info("[2/5] Capability Router: 已指定 skill=%s，跳过路由", state["prompt_skill"].name)
        return {"capability_route": {"route_type": "skill", "name": state["prompt_skill"].name, "mode": "fixed"}}
    candidates = state.get("skill_candidates") or []
    sample_spans = [
        {
            "source": span.source,
            "risk": span.risk,
            "text_preview": compact_snippet(span.text, 120),
            "text_length": len(span.text),
        }
        for span in state["spans"][:5]
    ]
    task_context = {
        "task": "rewrite_aigc_flagged_academic_passages",
        "document_type": "academic_paper",
        "language": "zh",
        "docx_path": str(state["docx_path"]),
        "report_path": str(state["report_path"]),
        "sample_spans": sample_spans,
    }
    review_options = state.get("review_options") or {}
    mcp_candidates = [human_review_candidate()] if review_options.get("enabled") else []
    LOGGER.info(
        "[2/5] Capability Router: 基于能力摘要选择下一步能力，skill 候选数量=%s mcp 候选数量=%s",
        len(candidates),
        len(mcp_candidates),
    )
    prompt_skill, route = route_capability(
        candidates,
        llm_options=state["llm_options"],
        task_context=task_context,
        mcp_candidates=mcp_candidates,
    )
    return {"prompt_skill": prompt_skill, "capability_route": route}


def _align_docx_paragraphs_node(state: AgentState) -> dict[str, Any]:
    LOGGER.info("[3/5] 解析论文 DOCX 并执行模糊对齐: %s", state["docx_path"])
    with DocxPackage(state["docx_path"]) as package:
        paragraphs = package.paragraphs()
    LOGGER.info("[3/5] 可见段落数量: %s", len(paragraphs))
    alignments = align_spans_to_docx(
        state["spans"],
        paragraphs,
        min_score=state["min_align_score"],
    )
    LOGGER.info("[3/5] 对齐完成: %s / %s 条风险片段命中", len(alignments), len(state["spans"]))
    for index, alignment in enumerate(alignments[:10], 1):
        LOGGER.debug(
            "对齐 %s: score=%.3f paragraph=%s text=%s",
            index,
            alignment.score,
            alignment.paragraph.key,
            compact_snippet(alignment.paragraph.text),
        )
    return {"alignments": alignments}


def _rewrite_docx_paragraphs_node(state: AgentState) -> dict[str, Any]:
    LOGGER.info("[4/5] 开始逐段改写与写回候选结果")
    skipped = list(state.get("skipped", []))
    review_pending = list(state.get("review_pending", []))
    rewritten_count = int(state.get("paragraphs_rewritten", 0))
    skill = state["rewrite_skill_factory"](state["prompt_skill"], state["llm_options"])
    total = len(state["alignments"])
    LOGGER.info("[4/5] 使用 skill=%s，待处理对齐段落: %s", state["prompt_skill"].name, total)
    review_options = state.get("review_options") or {}
    route = state.get("capability_route") or {}
    use_human_review = route.get("route_type") == "mcp" and route.get("name") == HUMAN_REVIEW_MCP_NAME
    review_decisions = load_review_decisions(review_options.get("decisions_json")) if use_human_review else {}
    if use_human_review:
        LOGGER.info(
            "[4/5] Router 选择人工审核 MCP: queue=%s decisions=%s decisions_loaded=%s",
            review_options.get("queue_jsonl"),
            review_options.get("decisions_json"),
            len(review_decisions),
        )

    with DocxPackage(state["docx_path"]) as package:
        for index, alignment in enumerate(state["alignments"], 1):
            original = alignment.paragraph.text
            LOGGER.info(
                "[4/5] 处理段落 %s/%s: key=%s score=%.3f length=%s",
                index,
                total,
                alignment.paragraph.key,
                alignment.score,
                len(original),
            )
            skip_reason = _skip_reason_before_rewrite(package, alignment)
            if skip_reason:
                skipped.append(skip_reason)
                LOGGER.info(
                    "[4/5] 跳过段落 %s/%s: reason=%s",
                    index,
                    total,
                    skip_reason["reason"],
                )
                continue

            final_text, final_issues = _rewrite_with_quality_retries(
                original=original,
                skill=skill,
                quality_options=state["quality_options"],
                max_retries=state["max_retries"],
                label=f"{index}/{total}",
            )
            if final_issues:
                skipped.append(
                    {
                        "paragraph": alignment.paragraph.key,
                        "reason": "quality_check_failed",
                        "issues": final_issues,
                        "score": alignment.score,
                        "text": compact_snippet(original),
                    }
                )
                LOGGER.warning("[4/5] 段落 %s/%s 质检失败，已跳过: %s", index, total, ", ".join(final_issues))
                continue

            if use_human_review:
                review_id = review_id_for(alignment.paragraph.key, original)
                approved = approved_text(review_decisions.get(review_id))
                if approved:
                    package.replace_paragraph(alignment.paragraph, approved)
                    rewritten_count += 1
                    LOGGER.info("[4/5] 段落 %s/%s 使用人工审核批准文本写回: review_id=%s", index, total, review_id)
                    continue
                queue_path = review_options.get("queue_jsonl") or "output/human_review_queue.jsonl"
                pending_item = {
                    "review_id": review_id,
                    "paragraph": alignment.paragraph.key,
                    "score": alignment.score,
                    "risk": alignment.span.risk,
                    "status": "pending",
                    "original": original,
                    "candidate": final_text,
                    "issues": final_issues,
                }
                append_review_queue_item(queue_path, pending_item)
                review_pending.append(
                    {
                        "review_id": review_id,
                        "paragraph": alignment.paragraph.key,
                        "score": alignment.score,
                        "queue_jsonl": str(queue_path),
                        "text": compact_snippet(original),
                    }
                )
                skipped.append(
                    {
                        "paragraph": alignment.paragraph.key,
                        "reason": "pending_human_review",
                        "review_id": review_id,
                        "score": alignment.score,
                        "queue_jsonl": str(queue_path),
                        "text": compact_snippet(original),
                    }
                )
                LOGGER.info("[4/5] 段落 %s/%s 已写入人工审核队列，暂不写回: review_id=%s", index, total, review_id)
                continue

            package.replace_paragraph(alignment.paragraph, final_text)
            rewritten_count += 1
            LOGGER.info("[4/5] 段落 %s/%s 改写通过，已加入写回队列", index, total)

        LOGGER.info("[4/5] 保存输出 DOCX: %s", state["output_docx"])
        package.save(state["output_docx"])

    LOGGER.info("[4/5] 改写节点完成: rewritten=%s skipped=%s", rewritten_count, len(skipped))
    return {
        "skipped": skipped,
        "review_pending": review_pending,
        "paragraphs_rewritten": rewritten_count,
        "skill_name": skill.name,
    }


def _write_process_report_node(state: AgentState) -> dict[str, Any]:
    LOGGER.info("[5/5] 生成处理过程 JSON: %s", state["report_json"])
    summary = AgentSummary(
        input_docx=str(state["docx_path"]),
        input_report=str(state["report_path"]),
        output_docx=str(state["output_docx"]),
        report_json=str(state["report_json"]),
        spans_found=len(state["spans"]),
        alignments_found=len(state["alignments"]),
        paragraphs_rewritten=state["paragraphs_rewritten"],
        skipped=state["skipped"],
    )
    payload = {
        "input_docx": summary.input_docx,
        "input_report": summary.input_report,
        "output_docx": summary.output_docx,
        "agent_framework": "langgraph",
        "skill_mode": "llm",
        "skill_name": state.get("skill_name", "llm"),
        "prompt_skill": state["prompt_skill"].name,
        "prompt_skill_source": state["prompt_skill"].source,
        "capability_route": state.get("capability_route"),
        "review": {
            "enabled": bool((state.get("review_options") or {}).get("enabled")),
            "pending": state.get("review_pending", []),
            "queue_jsonl": (state.get("review_options") or {}).get("queue_jsonl"),
            "decisions_json": (state.get("review_options") or {}).get("decisions_json"),
        },
        "llm_model": state["llm_options"].get("model"),
        "llm_base_url": state["llm_options"].get("base_url"),
        "spans_found": summary.spans_found,
        "alignments_found": summary.alignments_found,
        "paragraphs_rewritten": summary.paragraphs_rewritten,
        "spans": [
            {
                "page": span.page,
                "source": span.source,
                "risk": span.risk,
                "text": compact_snippet(span.text, 220),
            }
            for span in state["spans"]
        ],
        "skipped": state["skipped"],
    }
    state["report_json"].parent.mkdir(parents=True, exist_ok=True)
    state["report_json"].write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    LOGGER.info("[5/5] JSON 报告写入完成")
    return {"summary": summary}


def _skip_reason_before_rewrite(package: DocxPackage, alignment: Alignment) -> dict[str, Any] | None:
    original = alignment.paragraph.text
    if len(original.strip()) < 40:
        return {
            "paragraph": alignment.paragraph.key,
            "reason": "too_short",
            "text": compact_snippet(original),
        }
    if not package.can_replace_paragraph(alignment.paragraph):
        return {
            "paragraph": alignment.paragraph.key,
            "reason": "protected_ooxml_fragment",
            "score": alignment.score,
            "text": compact_snippet(original),
        }
    return None


def _rewrite_with_quality_retries(
    *,
    original: str,
    skill: LLMRewriteSkill,
    quality_options: dict[str, Any],
    max_retries: int,
    label: str,
) -> tuple[str, list[str]]:
    masked = mask_fragile_text(original)
    feedback: list[str] = []
    final_text = original
    final_issues: list[str] = []
    for attempt in range(1, max_retries + 2):
        LOGGER.info("调用 LLM 改写段落 %s，尝试 %s/%s", label, attempt, max_retries + 1)
        rewritten_masked = skill.rewrite(masked.text, feedback=feedback)
        candidate = unmask_text(rewritten_masked, masked.masks)
        issues = validate_rewrite_with_thresholds(
            original,
            candidate,
            masked.masks,
            **quality_options,
        )
        final_text = candidate
        final_issues = issues
        if not issues:
            LOGGER.info("段落 %s 质检通过: output_length=%s", label, len(candidate.strip()))
            break
        LOGGER.warning("段落 %s 质检未通过: %s", label, ", ".join(issues))
        feedback = issues
    return final_text, final_issues


def _default_rewrite_skill_factory(
    prompt_skill: PromptSkillSpec,
    llm_options: dict[str, Any],
) -> LLMRewriteSkill:
    return LLMRewriteSkill(prompt_skill=prompt_skill, **llm_options)
