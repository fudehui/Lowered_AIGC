# 智能体框架说明

项目选用 LangGraph 作为 AIGC 降重流程的编排层。

当前状态图很小，但边界清晰：

```text
extract_report_spans
  -> route_capability
  -> align_docx_paragraphs
  -> rewrite_docx_paragraphs
  -> write_process_report
```

## 为什么使用 LangGraph

这个项目不是简单的一次性文本转换，而是一个带状态的文档处理流程：需要读取检测报告、解析 DOCX、模糊对齐、逐段改写、质量检查、失败重试、跳过复杂 OOXML 段落，并最终输出处理报告。

LangGraph 适合把这些阶段拆成明确节点，让每一步输入输出都进入同一个状态对象。后续如果要加入人工复核、断点续跑、批量并行改写或更细的过程观测，也可以在现有图上继续扩展。

## 与 ReAct、Plan-and-Solve、Reflection 的关系

当前实现更接近 Plan-and-Solve，但不是“让 LLM 先生成计划再执行”的开放式版本，而是把计划固化为 LangGraph 状态图：

- Plan：`extract_report_spans`、`route_capability`、`align_docx_paragraphs`、`rewrite_docx_paragraphs`、`write_process_report`。
- Solve：每个节点按顺序执行，改写节点内部完成掩码、LLM 调用、质检重试和 DOCX 写回。

它不是 ReAct，因为当前流程不需要模型在运行中反复选择工具、观察工具结果、再决定下一步；报告解析、DOCX 对齐、LLM 改写和写回顺序都是确定的。

它也不是完整 Reflection，因为现在的反思能力来自规则化质量检查，例如长度比例、相似度和掩码保留情况。这个检查会驱动重试，但不会再调用一个 LLM 做自我批判。后续可以在图里加入 `reflect_rewrite_quality` 节点，把它扩展成 Plan-and-Solve + Reflection。

## 当前实现

公开入口保持不变：

```python
from lowered_aigc.core.agent import run_agent
```

内部实现改为：

```python
from lowered_aigc.core.langgraph_agent import run_langgraph_agent
```

CLI 无需改变调用方式：

```powershell
python -m lowered_aigc.cli --config lowered_aigc\config\default.yaml
```

如果环境中缺少 LangGraph，程序会提示安装依赖：

```powershell
python -m pip install -r requirements.txt
```

## Capability Router

`route_capability` 是能力路由节点。它不会把所有 skill 的完整内容发给改写模型，而是只发送能力摘要和少量短预览：

- skill 名称
- front matter 中的 description
- 由主要章节和描述压缩得到的 summary
- 当前任务上下文和少量风险片段短预览

LLM router 返回结构化 JSON：

```json
{
  "route_type": "skill",
  "name": "humanize-writing",
  "reason": "...",
  "need_full_content": true
}
```

`route_type` 可以是 `skill`、`tool`、`mcp` 或 `none`。如果选择 `skill`，系统才加载该 skill 的完整 `SKILL.md` 和 `references/*.md`，并交给后续 `rewrite_docx_paragraphs` 节点使用。如果选择 `none`，系统使用内置学术降 AIGC 规则继续改写。如果选择 `mcp: human-review`，系统会生成改写候选并写入人工审核队列，待批准后再写回。

如果 `skill.path` 指向单个 skill 目录，router 仍会收到摘要列表；如果模型返回不规范且候选只有一个，系统会回退到唯一候选。`--llm-debug-file` 中会用 `call_type=capability_router` 记录路由调用，用 `call_type=rewrite` 记录段落改写调用。

人工审核 MCP 由 `review.enabled` 开启。开启后，`human-review` 只是 MCP 候选之一，仍由 Capability Router 判断是否需要使用；默认队列文件是 `output/human_review_queue.jsonl`，决策文件是 `output/human_review_decisions.json`。队列记录包含 `review_id`、原文和 LLM 候选文本；决策文件中对应 `review_id` 的 `status=approved` 和 `approved_text` 会被用于写回 DOCX。

## 后续可扩展方向

- 为长文档处理增加 checkpoint，支持中断后继续运行。
- 把人工审核 MCP 接到外部审批 UI 或真实 MCP server。
- 对低风险、短段落或高置信对齐结果做批量改写。
- 在 JSON 报告中记录每个节点耗时、重试次数和失败原因。
