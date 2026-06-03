from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .config.loader import get_nested, load_config
from .core.agent import run_agent
from .reports.pdf import PdfReportError
from .rewrite.adapters import discover_prompt_skills


LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper AIGC reduction agent")
    parser.add_argument("--config", default=None, help="YAML config path")
    parser.add_argument("--docx", help="Input paper DOCX. Glob patterns are supported.")
    parser.add_argument("--report-file", help="AIGC report file, .docx or .pdf")
    parser.add_argument("--out", help="Output DOCX")
    parser.add_argument("--report", help="Processing report JSON")
    parser.add_argument("--model", help="LLM model name")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL, e.g. https://api.openai.com/v1")
    parser.add_argument("--api-key", help="LLM API key value. Prefer YAML or environment variables for regular use.")
    parser.add_argument("--api-key-env", help="Environment variable containing the LLM API key")
    parser.add_argument("--temperature", type=float, help="LLM sampling temperature")
    parser.add_argument("--max-output-tokens", type=int, help="LLM max_output_tokens")
    parser.add_argument("--llm-debug-file", help="Write each LLM request payload and raw response as JSONL")
    parser.add_argument("--skill-dir", help="External skill directory. Must contain SKILL.md")
    parser.add_argument("--enable-human-review", action="store_true", help="Add human-review to MCP candidates for the LLM router")
    parser.add_argument("--review-queue", help="Human review queue JSONL path")
    parser.add_argument("--review-decisions", help="Human review decisions JSON path")
    parser.add_argument("--max-spans", type=int)
    parser.add_argument("--min-align-score", type=float)
    parser.add_argument("--max-retries", type=int)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console log level. Default: INFO",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)
    config = load_config(args.config)

    report_file = args.report_file or get_nested(config, "paths.report_file")
    docx_value = args.docx or get_nested(config, "paths.input_docx")
    out_value = args.out or get_nested(config, "paths.output_docx")
    report_json_value = args.report or get_nested(config, "paths.process_report_json")

    try:
        if not docx_value:
            raise ValueError("--docx is required unless paths.input_docx is set in YAML")
        if not report_file:
            raise ValueError("--report-file is required unless paths.report_file is set in YAML")
        if not out_value:
            raise ValueError("--out is required unless paths.output_docx is set in YAML")
        if not report_json_value:
            raise ValueError("--report is required unless paths.process_report_json is set in YAML")

        docx_path = resolve_input_path(str(docx_value), label="input DOCX")
        report_path = resolve_input_path(str(report_file), label="AIGC report")
        output_docx = Path(str(out_value))
        report_json = Path(str(report_json_value))
        llm_options = _llm_options(config, args)
        max_spans = args.max_spans if args.max_spans is not None else get_nested(config, "report.max_spans", 80)
        min_align_score = (
            args.min_align_score
            if args.min_align_score is not None
            else get_nested(config, "alignment.min_score", 0.58)
        )
        max_retries = (
            args.max_retries
            if args.max_retries is not None
            else get_nested(config, "rewrite.max_retries", 2)
        )

        skill_info = _skill_info(config, args.skill_dir)
        LOGGER.info("发现外部 skill 候选: %s", skill_info["path"])
        skill_candidates = discover_prompt_skills(skill_info["path"], source=skill_info.get("source"))
        LOGGER.info("发现 %s 个 skill 候选: %s", len(skill_candidates), ", ".join(item.name for item in skill_candidates))
        LOGGER.info("启动 LangGraph AIGC 改写流程")
        LOGGER.info("输入论文: %s", docx_path)
        LOGGER.info("检测报告: %s", report_path)
        LOGGER.info("输出 DOCX: %s", output_docx)
        LOGGER.info("过程报告: %s", report_json)
        LOGGER.info(
            "LLM: model=%s base_url=%s key_source=%s",
            llm_options.get("model"),
            llm_options.get("base_url"),
            "llm.api_key" if llm_options.get("api_key") else f"env:{llm_options.get('api_key_env')}",
        )
        LOGGER.info("参数: max_spans=%s min_align_score=%s max_retries=%s", max_spans, min_align_score, max_retries)

        summary = run_agent(
            docx_path=docx_path,
            report_path=report_path,
            output_docx=output_docx,
            report_json=report_json,
            llm_options=llm_options,
            skill_candidates=skill_candidates,
            quality_options=_quality_options(config),
            review_options=_review_options(config, args),
            max_spans=max_spans,
            min_align_score=min_align_score,
            max_retries=max_retries,
        )
    except (PdfReportError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
        return 2

    print(
        json.dumps(
            {
                "output_docx": summary.output_docx,
                "report_json": summary.report_json,
                "spans_found": summary.spans_found,
                "alignments_found": summary.alignments_found,
                "paragraphs_rewritten": summary.paragraphs_rewritten,
                "skipped": len(summary.skipped),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _configure_logging(level: str) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    for noisy_logger in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def resolve_input_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if path.exists():
        return path
    if any(ch in value for ch in "*?[]"):
        matches = [Path(item) for item in glob.glob(value)]
        files = [item for item in matches if item.is_file()]
        if len(files) == 1:
            return files[0]
        if not files:
            raise ValueError(f"{label} did not match any file: {value}")
        names = ", ".join(str(item) for item in files[:5])
        raise ValueError(f"{label} matched multiple files; narrow the glob: {names}")
    raise ValueError(f"{label} does not exist: {value}")


def _llm_options(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    options = dict(get_nested(config, "llm", {}) or {})
    if args.model:
        options["model"] = args.model
    if args.base_url:
        options["base_url"] = args.base_url
    if args.api_key:
        options["api_key"] = args.api_key
    if args.api_key_env:
        options["api_key_env"] = args.api_key_env
    if args.temperature is not None:
        options["temperature"] = args.temperature
    if args.max_output_tokens is not None:
        options["max_output_tokens"] = args.max_output_tokens
    if args.llm_debug_file:
        options["debug_file"] = args.llm_debug_file

    for key in ("model", "base_url"):
        if not options.get(key):
            raise ValueError(f"Missing llm.{key} in YAML or --{key.replace('_', '-')}")
    if not options.get("api_key") and not options.get("api_key_env"):
        raise ValueError("Missing llm.api_key or llm.api_key_env in YAML, or --api-key / --api-key-env")
    return options


def _skill_info(config: dict[str, Any], cli_skill_dir: str | None) -> dict[str, str]:
    if cli_skill_dir:
        return {"path": cli_skill_dir, "source": "cli"}
    skill = get_nested(config, "skill", {}) or {}
    if not skill.get("path"):
        raise ValueError("Missing skill.path in YAML or --skill-dir")
    return skill


def _quality_options(config: dict[str, Any]) -> dict[str, Any]:
    quality = get_nested(config, "quality", {}) or {}
    return {
        "min_length_ratio": quality.get("min_length_ratio", 0.45),
        "max_length_ratio": quality.get("max_length_ratio", 1.8),
        "max_similarity": quality.get("max_similarity", 0.985),
    }


def _review_options(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    review = dict(get_nested(config, "review", {}) or {})
    if args.enable_human_review:
        review["enabled"] = True
    if args.review_queue:
        review["queue_jsonl"] = args.review_queue
    if args.review_decisions:
        review["decisions_json"] = args.review_decisions
    review.setdefault("enabled", False)
    review.setdefault("queue_jsonl", "output/human_review_queue.jsonl")
    review.setdefault("decisions_json", "output/human_review_decisions.json")
    return review


if __name__ == "__main__":
    raise SystemExit(main())
