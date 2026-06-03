from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..rewrite.adapters import CapabilityCandidate
from ..utils.text import compact_snippet


HUMAN_REVIEW_MCP_NAME = "human-review"


def human_review_candidate() -> CapabilityCandidate:
    return CapabilityCandidate(
        route_type="mcp",
        name=HUMAN_REVIEW_MCP_NAME,
        description="人工审核改写候选，不自动写回 DOCX。",
        summary=(
            "当论文段落风险较高、需要人工确认改写是否保留原意时使用。"
            "系统会生成 LLM 改写候选，写入审核队列；只有审核决策批准后才写回。"
        ),
    )


def review_id_for(paragraph_key: str, original: str) -> str:
    digest = hashlib.sha256(f"{paragraph_key}\n{original}".encode("utf-8")).hexdigest()
    return digest[:16]


def load_review_decisions(path: str | Path | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    decision_path = Path(path)
    if not decision_path.exists():
        return {}
    data = json.loads(decision_path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        if "decisions" in data and isinstance(data["decisions"], list):
            return {
                str(item.get("review_id")): item
                for item in data["decisions"]
                if isinstance(item, dict) and item.get("review_id")
            }
        return {str(key): value for key, value in data.items() if isinstance(value, dict)}
    if isinstance(data, list):
        return {
            str(item.get("review_id")): item
            for item in data
            if isinstance(item, dict) and item.get("review_id")
        }
    return {}


def approved_text(decision: dict[str, Any] | None) -> str | None:
    if not decision:
        return None
    status = str(decision.get("status") or decision.get("decision") or "").lower()
    if status not in {"approved", "approve", "accepted", "accept"}:
        return None
    text = decision.get("approved_text") or decision.get("text") or decision.get("candidate")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def append_review_queue_item(path: str | Path, item: dict[str, Any]) -> None:
    queue_path = Path(path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        **item,
        "original_preview": compact_snippet(str(item.get("original", "")), 220),
        "candidate_preview": compact_snippet(str(item.get("candidate", "")), 220),
    }
    with queue_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
