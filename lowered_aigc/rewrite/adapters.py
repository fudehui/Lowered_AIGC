from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


LOGGER = logging.getLogger(__name__)

MASK_PATTERNS = [
    re.compile(r"https?://\S+"),
    re.compile(r"\[[0-9,\-\s]+\]"),
    re.compile(r"（[0-9,\-\s]+）"),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|ms|s|m|cm|mm|kg|g|MB|GB|FPS|mAP)?\b"),
    re.compile(r"\b[A-Z][A-Za-z0-9_\-]{1,24}\b"),
]


@dataclass
class MaskedText:
    text: str
    masks: dict[str, str]


@dataclass(slots=True)
class PromptSkillSpec:
    name: str
    instructions: str
    references: dict[str, str]
    source: str | None = None


@dataclass(slots=True)
class PromptSkillCandidate:
    name: str
    path: Path
    description: str
    summary: str
    source: str | None = None


@dataclass(slots=True)
class CapabilityCandidate:
    route_type: str
    name: str
    description: str
    summary: str


def discover_prompt_skills(path: str | Path, *, source: str | None = None) -> list[PromptSkillCandidate]:
    root = Path(path)
    skill_dirs = [root] if (root / "SKILL.md").exists() else [item for item in sorted(root.iterdir()) if (item / "SKILL.md").exists()]
    if not skill_dirs:
        raise ValueError(f"No skill directories found under: {root}")
    return [_load_prompt_skill_candidate(skill_dir, source=source) for skill_dir in skill_dirs]


def load_prompt_skill(path: str | Path, *, source: str | None = None) -> PromptSkillSpec:
    skill_dir = Path(path)
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        raise ValueError(f"Skill file not found: {skill_file}")
    instructions = skill_file.read_text(encoding="utf-8")
    references: dict[str, str] = {}
    ref_dir = skill_dir / "references"
    if ref_dir.exists():
        for ref in sorted(ref_dir.glob("*.md")):
            references[ref.name] = ref.read_text(encoding="utf-8")
    return PromptSkillSpec(
        name=skill_dir.name,
        instructions=instructions,
        references=references,
        source=source,
    )


def route_capability(
    skill_candidates: list[PromptSkillCandidate],
    *,
    llm_options: dict,
    task_context: dict,
    tool_candidates: list[CapabilityCandidate] | None = None,
    mcp_candidates: list[CapabilityCandidate] | None = None,
) -> tuple[PromptSkillSpec, dict]:
    tool_candidates = tool_candidates or []
    mcp_candidates = mcp_candidates or []

    api_key = _resolve_api_key(llm_options)
    client = _openai_client(
        api_key=api_key,
        base_url=str(llm_options["base_url"]).rstrip("/"),
        timeout=int(llm_options.get("timeout", 120)),
    )
    payload = _capability_router_payload(
        skill_candidates,
        tool_candidates=tool_candidates,
        mcp_candidates=mcp_candidates,
        llm_options=llm_options,
        task_context=task_context,
    )
    LOGGER.info(
        "Capability Router: 发送能力摘要供 LLM 选择，skills=%s tools=%s mcps=%s",
        len(skill_candidates),
        len(tool_candidates),
        len(mcp_candidates),
    )
    LOGGER.debug("Capability Router 请求 payload:\n%s", json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        response = client.responses.create(**payload)
    except Exception as exc:
        raise RuntimeError(f"Capability Router LLM error: {exc}") from exc

    LOGGER.debug("Capability Router 原始响应:\n%s", _response_to_json(response))
    output_text = _extract_responses_output_text(response)
    LOGGER.debug("Capability Router 提取 output_text:\n%s", output_text)
    route = _parse_capability_route(output_text, skill_candidates, tool_candidates, mcp_candidates)
    LOGGER.info("Capability Router: route_type=%s name=%s reason=%s", route["route_type"], route.get("name"), route.get("reason"))
    _write_llm_debug_record(
        Path(llm_options["debug_file"]) if llm_options.get("debug_file") else None,
        call_type="capability_router",
        payload=payload,
        response=response,
        output_text=output_text,
    )
    if route["route_type"] == "skill":
        selected = next(candidate for candidate in skill_candidates if candidate.name == route["name"])
        return load_prompt_skill(selected.path, source=selected.source), route
    if route["route_type"] in {"none", "mcp"}:
        return _builtin_academic_rewrite_skill(), route
    raise RuntimeError(
        f"Capability route {route['route_type']}:{route.get('name')} was selected, "
        "but this project does not have an executor for that capability type yet."
    )


def route_prompt_skill(
    candidates: list[PromptSkillCandidate],
    *,
    llm_options: dict,
    task_context: dict,
) -> PromptSkillSpec:
    prompt_skill, _route = route_capability(candidates, llm_options=llm_options, task_context=task_context)
    return prompt_skill


def _load_prompt_skill_candidate(skill_dir: Path, *, source: str | None) -> PromptSkillCandidate:
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    metadata, body = _split_skill_front_matter(text)
    name = metadata.get("name") or skill_dir.name
    description = metadata.get("description") or _first_nonempty_line(body) or name
    summary = _compact_skill_summary(description=description, body=body)
    return PromptSkillCandidate(
        name=name,
        path=skill_dir,
        description=description,
        summary=summary,
        source=source,
    )


def _split_skill_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    metadata: dict[str, str] = {}
    key: str | None = None
    for raw_line in parts[1].splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value and value != "|":
                metadata[key] = value
            elif value == "|":
                metadata[key] = ""
        elif key and line.startswith(" "):
            metadata[key] = (metadata.get(key, "") + " " + line.strip()).strip()
    return metadata, parts[2].strip()


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip("# ").strip()
        if stripped:
            return stripped
    return ""


def _compact_skill_summary(*, description: str, body: str, max_chars: int = 1200) -> str:
    headings = [line.strip("# ").strip() for line in body.splitlines() if line.startswith("#")]
    parts = [description.strip()]
    if headings:
        parts.append("主要章节: " + "; ".join(headings[:12]))
    summary = "\n".join(part for part in parts if part)
    return summary[:max_chars]


def _capability_router_payload(
    skill_candidates: list[PromptSkillCandidate],
    *,
    tool_candidates: list[CapabilityCandidate],
    mcp_candidates: list[CapabilityCandidate],
    llm_options: dict,
    task_context: dict,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": llm_options["model"],
        "instructions": (
            "你是 Capability Router。根据任务上下文和候选能力摘要，选择下一步需要的能力。"
            "可选 route_type: skill、tool、mcp、none。不要为了选择而选择；"
            "如果内置学术改写规则已经足够，选择 none。"
            "只输出 JSON，例如 {\"route_type\":\"skill\",\"name\":\"humanize-writing\",\"reason\":\"...\",\"need_full_content\":true} "
            "或 {\"route_type\":\"none\",\"name\":null,\"reason\":\"...\",\"need_full_content\":false}。"
            "不要输出 Markdown。"
        ),
        "input": json.dumps(
            {
                "task": "select_capability_for_academic_aigc_rewrite",
                "task_context": task_context,
                "skills": [
                    {
                        "name": candidate.name,
                        "description": candidate.description,
                        "summary": candidate.summary,
                    }
                    for candidate in skill_candidates
                ],
                "tools": [
                    {
                        "name": candidate.name,
                        "description": candidate.description,
                        "summary": candidate.summary,
                    }
                    for candidate in tool_candidates
                ],
                "mcps": [
                    {
                        "name": candidate.name,
                        "description": candidate.description,
                        "summary": candidate.summary,
                    }
                    for candidate in mcp_candidates
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
    }
    if llm_options.get("temperature") is not None:
        payload["temperature"] = min(float(llm_options.get("temperature", 0.0)), 0.2)
    payload["max_output_tokens"] = 300
    return payload


def _parse_capability_route(
    output_text: str,
    skill_candidates: list[PromptSkillCandidate],
    tool_candidates: list[CapabilityCandidate],
    mcp_candidates: list[CapabilityCandidate],
) -> dict:
    skills = {candidate.name for candidate in skill_candidates}
    tools = {candidate.name for candidate in tool_candidates}
    mcps = {candidate.name for candidate in mcp_candidates}
    try:
        data = json.loads(output_text)
        route_type = data.get("route_type") or ("skill" if data.get("skill_name") else None)
        name = data.get("name") or data.get("skill_name")
        route = {
            "route_type": route_type,
            "name": name,
            "reason": data.get("reason", ""),
            "need_full_content": bool(data.get("need_full_content", route_type == "skill")),
        }
        if _is_valid_capability_route(route, skills, tools, mcps):
            return route
    except Exception:
        pass
    lowered = output_text.lower()
    if '"none"' in lowered or "route_type" in lowered and "none" in lowered:
        return {
            "route_type": "none",
            "name": None,
            "reason": "router_output_selected_none",
            "need_full_content": False,
        }
    for name in skills:
        if name in output_text:
            return {
                "route_type": "skill",
                "name": name,
                "reason": "router_output_mentioned_skill_name",
                "need_full_content": True,
            }
    if len(skill_candidates) == 1 and not tool_candidates and not mcp_candidates:
        LOGGER.warning("Capability Router: 响应未给出合法路由，回退到唯一 skill 候选 %s", skill_candidates[0].name)
        return {
            "route_type": "skill",
            "name": skill_candidates[0].name,
            "reason": "fallback_to_only_skill_candidate",
            "need_full_content": True,
        }
    return {
        "route_type": "none",
        "name": None,
        "reason": f"router_output_unparseable: {output_text[:200]}",
        "need_full_content": False,
    }


def _is_valid_capability_route(route: dict, skills: set[str], tools: set[str], mcps: set[str]) -> bool:
    route_type = route.get("route_type")
    name = route.get("name")
    if route_type == "none":
        return True
    if route_type == "skill":
        return name in skills
    if route_type == "tool":
        return name in tools
    if route_type == "mcp":
        return name in mcps
    return False


def _builtin_academic_rewrite_skill() -> PromptSkillSpec:
    return PromptSkillSpec(
        name="builtin-academic-rewrite",
        source="builtin:none",
        references={},
        instructions=(
            "使用内置论文降 AIGC 改写规则：保持原意、术语、数据、引用、模型名和图表指代不变；"
            "减少模板化表达、机械连接词、过度抽象表述和 AI 腔；"
            "调整句式、语序和节奏，但不要新增事实、实验、文献、结论或评价。"
        ),
    )


def mask_fragile_text(text: str) -> MaskedText:
    masks: dict[str, str] = {}

    def repl(match: re.Match[str]) -> str:
        token = f"__MASK_{len(masks):04d}__"
        masks[token] = match.group(0)
        return token

    masked = text
    for pattern in MASK_PATTERNS:
        masked = pattern.sub(repl, masked)
    return MaskedText(masked, masks)


def unmask_text(text: str, masks: dict[str, str]) -> str:
    for token, value in masks.items():
        text = text.replace(token, value)
    return text


class LLMRewriteSkill:
    """LLM rewriter using the OpenAI SDK against a Responses-compatible API."""

    name = "llm"

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        prompt_skill: PromptSkillSpec,
        api_key: str | None = None,
        api_key_env: str | None = None,
        debug_file: str | None = None,
        timeout: int = 120,
        temperature: float | None = 0.35,
        max_output_tokens: int | None = 1200,
    ):
        self.model = model
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.prompt_skill = prompt_skill
        self.debug_file = Path(debug_file) if debug_file else None
        self.timeout = timeout
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens

    def rewrite(self, text: str, feedback: list[str] | None = None) -> str:
        api_key = _resolve_api_key({"api_key": self.api_key, "api_key_env": self.api_key_env})

        client = _openai_client(api_key=api_key, base_url=self.base_url, timeout=self.timeout)
        payload: dict[str, object] = {
            "model": self.model,
            "instructions": self._instructions(feedback),
            "input": self._input_text(text, feedback),
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_output_tokens is not None:
            payload["max_output_tokens"] = self.max_output_tokens

        LOGGER.info(
            "发送 LLM 请求: provider=openai-sdk model=%s base_url=%s input_chars=%s instruction_chars=%s",
            self.model,
            self.base_url,
            len(text),
            len(str(payload["instructions"])),
        )
        LOGGER.debug("LLM 请求 payload:\n%s", json.dumps(payload, ensure_ascii=False, indent=2))

        try:
            response = client.responses.create(**payload)
        except Exception as exc:
            raise RuntimeError(f"LLM Responses API error: {exc}") from exc

        LOGGER.debug("LLM 原始响应:\n%s", _response_to_json(response))
        output_text = _extract_responses_output_text(response)
        if not output_text:
            raise RuntimeError(f"LLM response did not contain output text: {_response_preview(response)}")
        _write_llm_debug_record(
            self.debug_file,
            call_type="rewrite",
            payload=payload,
            response=response,
            output_text=output_text,
        )
        LOGGER.info("收到 LLM 响应: output_chars=%s", len(output_text.strip()))
        LOGGER.debug("LLM 提取 output_text:\n%s", output_text)
        return output_text.strip()

    def _instructions(self, feedback: list[str] | None) -> str:
        parts = [
            "你是论文降 AIGC 改写助手。只输出改写后的段落正文，不要解释，不要 Markdown，不要标题。",
            "必须保持原意、论文学术事实、术语、数字、引用、公式占位符、模型名、图表引用不变。",
            "不要新增文献、数据、实验、结论或评价。不要删除 __MASK_0000__ 这类掩码 token。",
            "改写目标是降低 AI 生成痕迹：调整句式、语序、节奏和连接方式，而不是机械同义词替换。",
            "下面是外部现成 skill 的完整说明，必须遵循：",
            self.prompt_skill.instructions,
        ]
        for name, content in self.prompt_skill.references.items():
            parts.append(f"外部 skill 参考资料 {name}：\n{content}")
        if feedback:
            parts.append("上一次质量检查反馈：" + ", ".join(feedback))
        return "\n\n".join(parts)

    def _input_text(self, text: str, feedback: list[str] | None) -> str:
        return json.dumps(
            {
                "task": "rewrite_one_aigc_flagged_academic_passage",
                "output_rule": "只输出改写后的文本本身",
                "preserve": [
                    "__MASK_0000__ 这类掩码 token",
                    "引用、数字、单位、模型名、公式引用、图表引用",
                    "原段落的事实、含义和学术边界",
                ],
                "quality_feedback": feedback or [],
                "text": text,
            },
            ensure_ascii=False,
            indent=2,
        )


def _openai_client(*, api_key: str, base_url: str, timeout: int):
    try:
        from openai import OpenAI
    except ImportError:
        OpenAI = _import_openai_from_user_site()
    return OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)


def _resolve_api_key(options: dict) -> str:
    api_key = options.get("api_key") or (os.environ.get(options.get("api_key_env")) if options.get("api_key_env") else None)
    if api_key:
        return str(api_key)
    if options.get("api_key_env"):
        raise ValueError(f"Missing LLM API key environment variable: {options.get('api_key_env')}")
    raise ValueError("Missing LLM API key. Set llm.api_key or llm.api_key_env.")


def _import_openai_from_user_site():
    import site
    import sys

    user_site = site.getusersitepackages()
    if user_site and user_site not in sys.path:
        sys.path.append(user_site)
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI SDK is required for LLM requests. "
            "Install dependencies with: python -m pip install -r requirements.txt"
        ) from exc
    return OpenAI


def _extract_responses_output_text(response: object) -> str:
    if hasattr(response, "model_dump"):
        output_text = _extract_responses_output_text_from_dict(response.model_dump())
        if output_text:
            return output_text

    if isinstance(response, dict):
        return _extract_responses_output_text_from_dict(response)

    try:
        output_text = getattr(response, "output_text", None)
    except Exception:
        output_text = None
    if isinstance(output_text, str):
        return output_text

    return ""


def _extract_responses_output_text_from_dict(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    chunks: list[str] = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "".join(chunks)


def _response_preview(response: object) -> str:
    return _response_to_json(response)[:800]


def _response_to_json(response: object) -> str:
    if hasattr(response, "model_dump_json"):
        return response.model_dump_json(indent=2)
    if hasattr(response, "model_dump"):
        return json.dumps(response.model_dump(), ensure_ascii=False, indent=2)
    if isinstance(response, dict):
        return json.dumps(response, ensure_ascii=False, indent=2)
    return repr(response)


def _write_llm_debug_record(
    debug_file: Path | None,
    *,
    call_type: str,
    payload: dict[str, object],
    response: object,
    output_text: str,
) -> None:
    if not debug_file:
        return
    debug_file.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "call_type": call_type,
        "request_payload": payload,
        "raw_response": _response_to_data(response),
        "output_text": output_text,
    }
    with debug_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _response_to_data(response: object) -> object:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if isinstance(response, dict):
        return response
    return repr(response)
