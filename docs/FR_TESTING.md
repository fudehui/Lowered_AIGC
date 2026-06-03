# 当前主线测试说明

本项目现在只保留一条主线：

```text
检测报告 -> DOCX 对齐 -> 外部 humanize-writing skill -> YAML 配置 LLM -> 原位回填 DOCX
```

以下测试围绕最终效果展开：读取 AIGC 文本、调用 LLM 改写、写回 Word，并尽量保持文献引用上标、公式、图片等格式不变。

## 0. 环境准备

```powershell
cd D:\Lowered_AIGC
$env:OPENAI_API_KEY="sk-..."
```

如果报告是 PDF：

```powershell
python -m pip install -r requirements.txt
```

## 1. 配置加载测试

```powershell
$env:PYTHONIOENCODING="utf-8"
@'
from lowered_aigc.config.loader import load_config
c = load_config()
print(c["skill"]["path"])
print(c["llm"]["base_url"])
print(c["llm"]["api_key_env"])
print(c["llm"]["model"])
'@ | python -
```

通过标准：

- skill 路径是 `lowered_aigc/skills/external/humanize-writing`
- `base_url / api_key_env / model` 都非空。

## 2. 检测报告读取测试

DOCX 报告：

```powershell
$env:PYTHONIOENCODING="utf-8"
@'
from pathlib import Path
from lowered_aigc.reports.extract import extract_report_spans

report = next(Path(".").glob("AIGC*.docx"))
spans = extract_report_spans(report, max_spans=10)
print("count =", len(spans))
for i, span in enumerate(spans[:5], 1):
    print(i, span.source, span.risk, span.text[:100].replace("\n", " "))
'@ | python -
```

PDF 报告：

```powershell
$env:PYTHONIOENCODING="utf-8"
@'
from pathlib import Path
from lowered_aigc.reports.extract import extract_report_spans

report = next(Path(".").glob("AIGC*.pdf"))
spans = extract_report_spans(report, max_spans=10)
print("count =", len(spans))
for i, span in enumerate(spans[:5], 1):
    print(i, span.source, span.risk, span.text[:100].replace("\n", " "))
'@ | python -
```

通过标准：

- `count > 0`
- DOCX 报告通常输出 `docx_color_text`
- PDF 报告可能输出 `pdf_highlight_annotation` 或 `pdf_text_fallback`

## 3. DOCX 对齐与保护测试

```powershell
$env:PYTHONIOENCODING="utf-8"
@'
from pathlib import Path
from lowered_aigc.reports.extract import extract_report_spans
from lowered_aigc.docx.package import DocxPackage
from lowered_aigc.core.alignment import align_spans_to_docx

paper = next(Path(".").glob("[!A]*.docx"))
report = next(Path(".").glob("AIGC*.docx"))
spans = extract_report_spans(report, max_spans=20)

with DocxPackage(paper) as package:
    paragraphs = package.paragraphs()
    alignments = align_spans_to_docx(spans, paragraphs, min_score=0.58)
    protected = [a for a in alignments if not package.can_replace_paragraph(a.paragraph)]

print("spans =", len(spans))
print("paragraphs =", len(paragraphs))
print("alignments =", len(alignments))
print("protected_alignments =", len(protected))
for item in alignments[:5]:
    print(round(item.score, 3), item.paragraph.key, item.paragraph.text[:100].replace("\n", " "))
'@ | python -
```

通过标准：

- `spans > 0`
- `paragraphs > 0`
- `alignments > 0`
- 命中公式/图片/域代码的段落会被识别为 protected，后续不写回。

## 4. 掩码保护测试

```powershell
$env:PYTHONIOENCODING="utf-8"
@'
from lowered_aigc.rewrite.adapters import mask_fragile_text, unmask_text

text = "本文采用 YOLOv11，在 mAP@0.5 达到 96.3%，如图2.1 所示，并参考文献[12-15]。"
masked = mask_fragile_text(text)
restored = unmask_text(masked.text, masked.masks)
print("masked =", masked.text)
print("masks =", masked.masks)
print("same =", restored == text)
'@ | python -
```

通过标准：

- `masks` 非空
- `same = True`

## 5. LLM 小样本真实调用测试

建议先只处理 1 到 3 个片段，避免一次消耗过多。

```powershell
python -m lowered_aigc.cli `
  --docx "[!A]*.docx" `
  --report-file "AIGC*.docx" `
  --out "output\llm_smoke.docx" `
  --report "output\llm_smoke.json" `
  --max-spans 3
```

通过标准：

- 命令退出码为 `0`
- `output\llm_smoke.docx` 存在
- `output\llm_smoke.json` 中：
  - `skill_mode = llm`
  - `skill_name = llm`
  - `prompt_skill = humanize-writing`
  - `paragraphs_rewritten > 0`

如果没有 API key，会报：

```text
Missing LLM API key environment variable: OPENAI_API_KEY
```

这是正常的配置错误，不是 DOCX 流程错误。

## 6. 全量运行

确认小样本 OK 后，再扩大范围：

```powershell
python -m lowered_aigc.cli `
  --docx "[!A]*.docx" `
  --report-file "AIGC*.docx" `
  --out "output\lowered.docx" `
  --report "output\lowered_aigc_report.json" `
  --max-spans 80
```

## 7. DOCX 结构验证

```powershell
python -c "import zipfile; z=zipfile.ZipFile('output/lowered.docx'); print('zip_bad', z.testzip()); print('entries', len(z.namelist()))"
```

通过标准：

```text
zip_bad None
```

结构计数对比：

```powershell
$env:PYTHONIOENCODING="utf-8"
@'
import zipfile
from pathlib import Path

src = next(p for p in Path(".").glob("[!A]*.docx") if p.is_file())
out = Path("output/lowered.docx")

for label, path in [("src", src), ("out", out)]:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        media = [n for n in names if n.startswith("word/media/")]
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
        print(label, "entries", len(names), "media", len(media), "oMath", xml.count("<m:oMath"), "drawing", xml.count("<w:drawing"))
'@ | python -
```

通过标准：

- `media` 数量一致
- `oMath` 数量一致
- `drawing` 数量一致

## 8. Word 打开验证

如果安装了 Microsoft Word：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\word_export_pdf.ps1 `
  -InputDocx output\lowered.docx `
  -OutputPdf output\lowered_word_export.pdf
```

通过标准：

- 命令退出码为 `0`
- PDF 文件存在且大小大于 `0`

## 9. 测试记录模板

```text
测试日期：
测试人：
模型：
base_url：
输入论文：
输入报告：

报告读取：通过/失败，证据：
DOCX 对齐：通过/失败，证据：
掩码保护：通过/失败，证据：
LLM 小样本：通过/失败，证据：
全量运行：通过/失败，证据：
结构验证：通过/失败，证据：
Word 导出：通过/失败，证据：

输出 DOCX：
输出 JSON：
遗留问题：
```

