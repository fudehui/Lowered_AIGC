# Lowered AIGC

Lowered AIGC 是一个面向学术论文 DOCX 的 AIGC 风险段落改写工具。它可以读取 AIGC 检测报告中标记的高风险或中风险文本，将这些文本自动对齐到原始 Word 论文段落，再通过 LLM 进行保持语义和学术事实不变的改写，最后将结果写回新的 DOCX 文件。

项目采用 LangGraph / LangChain 思路做流程编排，OpenAI SDK 负责模型请求；同时引入 Capability Router，让 LLM 根据候选 skill / tool / MCP 摘要判断下一步应该使用哪种能力，而不是把所有能力内容一次性塞入上下文。

## 中文说明

### 项目特点

- 支持从 `.docx` 或 `.pdf` AIGC 检测报告中抽取风险片段。
- 支持将风险片段模糊对齐到原论文 DOCX 段落。
- 只改写检测报告命中的段落，不重写整篇论文。
- 使用 LangGraph 编排处理流程，节点边界清晰，便于扩展。
- 使用 OpenAI SDK 调用 OpenAI-compatible Responses API。
- 支持外部 skill，例如 `humanize-writing`。
- 支持 Capability Router，由 LLM 判断使用 `skill`、`tool`、`mcp` 或 `none`。
- 支持人工审核 MCP 候选：当 Router 选择 `human-review` 时，候选改写会进入审核队列。
- 对数字、引用、URL、模型名等敏感信息做掩码保护。
- 遇到公式、图片、域代码、复杂 OOXML 段落时自动跳过，尽量保护 Word 格式。
- 输出 JSON 过程报告，记录抽取、对齐、改写、跳过原因和路由结果。

### 智能体流程

当前主流程由 LangGraph 状态图编排：

```text
extract_report_spans
  -> route_capability
  -> align_docx_paragraphs
  -> rewrite_docx_paragraphs
  -> write_process_report
```

各节点职责：

| 节点 | 说明 |
|---|---|
| `extract_report_spans` | 从 AIGC 检测报告中抽取风险文本。 |
| `route_capability` | 将任务上下文、候选能力摘要和短文本预览交给 LLM，由 LLM 判断使用哪种能力。 |
| `align_docx_paragraphs` | 将风险片段对齐到原始 DOCX 段落。 |
| `rewrite_docx_paragraphs` | 对命中段落做掩码保护、LLM 改写、质量检查、重试和写回。 |
| `write_process_report` | 生成处理过程 JSON 报告。 |

当前更接近 Plan-and-Solve 风格：整体计划由 LangGraph 状态图固定，具体改写由 LLM 完成。项目没有采用完整 ReAct，因为文档处理顺序比较确定；也没有采用完整 Reflection，因为目前的自检主要是规则化质量检查和重试。

### 项目结构

```text
lowered_aigc/
  cli.py                         # 命令行入口
  config/
    default.yaml                 # 默认配置
    loader.py                    # YAML 配置加载
  core/
    agent.py                     # 对外主入口
    langgraph_agent.py           # LangGraph 状态图
    human_review.py              # 人工审核 MCP 队列
    alignment.py                 # 风险文本与 DOCX 段落对齐
    quality.py                   # 改写质量检查
    models.py                    # 核心数据结构
  docx/
    package.py                   # DOCX OOXML 解析与写回
  reports/
    extract.py                   # 报告类型分流
    docx.py                      # DOCX 报告解析
    pdf.py                       # PDF 报告解析
  rewrite/
    adapters.py                  # LLM 调用、skill 加载、掩码保护、Capability Router
  skills/
    external/humanize-writing/   # 外部 humanize-writing skill
  utils/
    text.py                      # 文本清洗与相似度工具
docs/
  AGENT_FRAMEWORK.md             # 智能体架构说明
  FR_TESTING.md                  # 分项测试记录
scripts/
  run_sample.ps1                 # 示例运行脚本
  word_export_pdf.ps1            # Word 导出 PDF 验证脚本
tests/
  mock_responses_api.py          # 本地 mock Responses API
```

### 安装

建议使用虚拟环境：

```powershell
cd D:\Lowered_AIGC
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

依赖：

- `langgraph`
- `openai`
- `PyYAML`
- `pypdf`
- `cryptography`

### 配置

默认配置文件：

```text
lowered_aigc/config/default.yaml
```

关键配置项：

| 配置 | 说明 |
|---|---|
| `paths.input_docx` | 输入论文 DOCX。 |
| `paths.report_file` | AIGC 检测报告，支持 DOCX / PDF。 |
| `paths.output_docx` | 输出 DOCX 路径。 |
| `paths.process_report_json` | 处理过程 JSON 路径。 |
| `report.max_spans` | 最多处理的风险片段数量。 |
| `alignment.min_score` | 模糊对齐阈值，越高越保守。 |
| `skill.path` | 外部 skill 目录。 |
| `llm.model` | 模型名称。 |
| `llm.base_url` | OpenAI-compatible API 地址。 |
| `llm.api_key` | API Key 本身，可留空。 |
| `llm.api_key_env` | API Key 环境变量名。 |
| `rewrite.max_retries` | 质量检查失败后的最大重试次数。 |
| `review.enabled` | 是否把 `human-review` 作为 MCP 候选交给 Router。 |

推荐使用环境变量保存 API Key：

```powershell
$env:OPENAI_API_KEY="your-api-key"
```

不要把真实 API Key 提交到 GitHub。

### 快速运行

```powershell
python -m lowered_aigc.cli `
  --docx "初稿5(1).docx" `
  --report-file "AIGC检测报告_[初稿5(1)].docx" `
  --out "output\lowered.docx" `
  --report "output\lowered_aigc_report.json" `
  --api-key-env "OPENAI_API_KEY"
```

指定模型和兼容 API 地址：

```powershell
python -m lowered_aigc.cli `
  --docx "paper.docx" `
  --report-file "aigc_report.pdf" `
  --out "output\lowered.docx" `
  --report "output\lowered_aigc_report.json" `
  --model "gpt-4.1-mini" `
  --base-url "https://api.openai.com/v1" `
  --api-key-env "OPENAI_API_KEY"
```

使用 YAML 配置运行：

```powershell
python -m lowered_aigc.cli --config lowered_aigc\config\default.yaml
```

### 查看 LLM 请求与响应

控制台打印调试日志：

```powershell
python -m lowered_aigc.cli `
  --docx "paper.docx" `
  --report-file "aigc_report.pdf" `
  --out "output\debug.docx" `
  --report "output\debug.json" `
  --max-spans 1 `
  --log-level DEBUG
```

写入 JSONL 调试文件：

```powershell
python -m lowered_aigc.cli `
  --docx "paper.docx" `
  --report-file "aigc_report.pdf" `
  --out "output\debug.docx" `
  --report "output\debug.json" `
  --max-spans 1 `
  --llm-debug-file "output\llm_calls.jsonl"
```

`llm_calls.jsonl` 会记录：

- `call_type`
- `request_payload`
- `raw_response`
- `output_text`

### Capability Router

Router 阶段不会发送所有 skill 的完整内容，而是只发送候选能力摘要和风险文本短预览。LLM 需要返回类似：

```json
{
  "route_type": "skill",
  "name": "humanize-writing",
  "reason": "适合处理 AI 写作痕迹",
  "need_full_content": true
}
```

支持的 `route_type`：

| 类型 | 说明 |
|---|---|
| `skill` | 加载被选中的完整 skill，并进入改写流程。 |
| `tool` | 预留工具能力扩展。 |
| `mcp` | 预留 MCP 能力扩展，目前支持 `human-review`。 |
| `none` | 使用内置学术改写规则。 |

开启人工审核 MCP 候选：

```yaml
review:
  enabled: true
  queue_jsonl: output/human_review_queue.jsonl
  decisions_json: output/human_review_decisions.json
```

注意：`human-review` 只是候选能力，是否使用仍由 LLM Router 判断。

### 本地烟测

启动 mock Responses API：

```powershell
python tests\mock_responses_api.py
```

另开终端运行：

```powershell
$env:OPENAI_API_KEY="mock-key"
python -m lowered_aigc.cli `
  --docx "paper.docx" `
  --report-file "aigc_report.docx" `
  --out "output\smoke.docx" `
  --report "output\smoke.json" `
  --base-url "http://127.0.0.1:8765/v1" `
  --model "mock-model" `
  --max-spans 1
```

### 输出文件

| 文件 | 说明 |
|---|---|
| `output/lowered.docx` | 改写后的论文 DOCX。 |
| `output/lowered_aigc_report.json` | 处理过程报告。 |
| `output/llm_calls.jsonl` | 可选，LLM 调用调试记录。 |
| `output/human_review_queue.jsonl` | 可选，人工审核队列。 |

### 常见问题

#### 缺少 API Key

设置环境变量：

```powershell
$env:OPENAI_API_KEY="your-api-key"
```

或在 YAML 中配置 `llm.api_key`。上传 GitHub 前请确认没有真实密钥。

#### 413 Payload Too Large

这表示请求体过大，被服务端或代理拒绝。常见原因是完整 skill、references 或待处理文本过长。可以减少 `max_spans`，缩短 skill 内容，或调整服务端请求体限制。

#### 对齐结果太少

降低 `alignment.min_score`，例如：

```powershell
--min-align-score 0.50
```

#### 误匹配较多

提高 `alignment.min_score`，例如：

```powershell
--min-align-score 0.65
```

### 许可证

请根据实际使用场景补充项目许可证。外部 skill 目录中的第三方内容请保留其原始许可证说明。

---

## English Version

Lowered AIGC is a DOCX-oriented rewriting tool for academic papers. It reads high-risk or medium-risk passages from an AIGC detection report, aligns those passages back to the original Word document, rewrites only the matched paragraphs with an LLM, and writes the result into a new DOCX file.

The project uses LangGraph-style orchestration for the workflow and the OpenAI SDK for model requests. It also includes a Capability Router, allowing the LLM to choose among candidate skills, tools, MCP capabilities, or built-in rules based on concise capability summaries.

### Features

- Extract risk passages from DOCX or PDF AIGC reports.
- Align report passages to paragraphs in the original DOCX.
- Rewrite only matched risk paragraphs instead of the whole paper.
- Preserve academic meaning, terminology, numbers, citations, model names, and document structure as much as possible.
- Skip complex OOXML paragraphs containing formulas, images, field codes, or embedded objects.
- Mask fragile text such as numbers, references, URLs, and model names before rewriting.
- Use OpenAI-compatible Responses API through the OpenAI SDK.
- Use LangGraph to organize the workflow into clear, extensible nodes.
- Support external prompt skills such as `humanize-writing`.
- Support Capability Router with `skill`, `tool`, `mcp`, and `none` route types.
- Support an optional `human-review` MCP candidate for manual approval workflows.
- Generate a JSON process report for debugging and auditability.

### Workflow

```text
extract_report_spans
  -> route_capability
  -> align_docx_paragraphs
  -> rewrite_docx_paragraphs
  -> write_process_report
```

### Installation

```powershell
cd D:\Lowered_AIGC
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### Configuration

Default config file:

```text
lowered_aigc/config/default.yaml
```

Recommended API key setup:

```powershell
$env:OPENAI_API_KEY="your-api-key"
```

Do not commit real API keys to GitHub.

### Quick Start

```powershell
python -m lowered_aigc.cli `
  --docx "paper.docx" `
  --report-file "aigc_report.pdf" `
  --out "output\lowered.docx" `
  --report "output\lowered_aigc_report.json" `
  --model "gpt-4.1-mini" `
  --base-url "https://api.openai.com/v1" `
  --api-key-env "OPENAI_API_KEY"
```

Run with YAML config:

```powershell
python -m lowered_aigc.cli --config lowered_aigc\config\default.yaml
```

### Debug LLM Calls

```powershell
python -m lowered_aigc.cli `
  --docx "paper.docx" `
  --report-file "aigc_report.pdf" `
  --out "output\debug.docx" `
  --report "output\debug.json" `
  --max-spans 1 `
  --log-level DEBUG `
  --llm-debug-file "output\llm_calls.jsonl"
```

The JSONL debug file records request payloads, raw responses, extracted output text, and call types.

### Capability Router

The router receives capability summaries and short text previews. It then asks the LLM to choose the next capability:

```json
{
  "route_type": "skill",
  "name": "humanize-writing",
  "reason": "Best suited for reducing AI-writing traces.",
  "need_full_content": true
}
```

Supported route types:

| Route Type | Meaning |
|---|---|
| `skill` | Load the selected full skill and use it for rewriting. |
| `tool` | Reserved for tool executors. |
| `mcp` | Reserved for MCP executors; currently includes `human-review`. |
| `none` | Use built-in academic rewriting rules. |

### Outputs

| File | Description |
|---|---|
| `output/lowered.docx` | Rewritten DOCX file. |
| `output/lowered_aigc_report.json` | Process report. |
| `output/llm_calls.jsonl` | Optional LLM call debug log. |
| `output/human_review_queue.jsonl` | Optional manual review queue. |

### Notes

- This tool is designed to assist academic editing, not to fabricate data, references, or conclusions.
- Always review generated text before formal submission.
- Keep API keys and unpublished papers out of public repositories unless you intentionally want to publish them.
