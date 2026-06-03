from __future__ import annotations

import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
import html
import re
from xml.etree import ElementTree as ET

from ..core.models import DocxParagraph


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
V_NS = "urn:schemas-microsoft-com:vml"

NS = {
    "w": W_NS,
    "m": M_NS,
    "wps": WPS_NS,
    "wp": WP_NS,
    "v": V_NS,
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}

for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)


SKIP_ANCESTOR_LOCAL_NAMES = {
    "oMath",
    "oMathPara",
    "drawing",
    "pict",
    "object",
    "fldSimple",
    "smartTag",
    "sdt",
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


@dataclass
class _ParagraphRecord:
    paragraph: ET.Element
    text_nodes: list[ET.Element]
    raw_index: int

    @property
    def text(self) -> str:
        return "".join(node.text or "" for node in self.text_nodes)


class DocxPackage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="lowered_aigc_docx_"))
        self._raw_parts: dict[str, str] = {}
        self._parts: dict[str, ET.ElementTree] = {}
        self._paragraphs: dict[str, list[_ParagraphRecord]] = {}
        self._replacements: dict[tuple[str, int], str] = {}
        self._loaded = False

    def close(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self) -> "DocxPackage":
        self.load()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def load(self) -> None:
        if self._loaded:
            return
        with zipfile.ZipFile(self.path) as zf:
            zf.extractall(self._tmpdir)
        for part in self._word_xml_parts():
            xml_path = self._tmpdir / part
            raw_xml = xml_path.read_text(encoding="utf-8")
            self._raw_parts[part] = raw_xml
            try:
                tree = ET.parse(xml_path)
            except ET.ParseError:
                continue
            self._parts[part] = tree
            self._paragraphs[part] = self._collect_paragraphs(tree.getroot(), raw_xml)
        self._loaded = True

    def paragraphs(self) -> list[DocxParagraph]:
        self.load()
        result: list[DocxParagraph] = []
        for part_name, records in self._paragraphs.items():
            for index, record in enumerate(records):
                text = record.text
                if text.strip():
                    result.append(DocxParagraph(part_name=part_name, index=index, text=text))
        return result

    def replace_paragraph(self, paragraph: DocxParagraph, new_text: str) -> None:
        self.load()
        self._replacements[(paragraph.part_name, paragraph.index)] = new_text

    def can_replace_paragraph(self, paragraph: DocxParagraph) -> bool:
        self.load()
        records = self._paragraphs.get(paragraph.part_name, [])
        if paragraph.index >= len(records):
            return False
        record = records[paragraph.index]
        matches = _paragraph_matches(self._raw_parts[paragraph.part_name])
        if record.raw_index >= len(matches):
            return False
        return PROTECTED_FRAGMENT_RE.search(matches[record.raw_index].group(0)) is None

    def save(self, out_path: str | Path) -> None:
        self.load()
        for part_name, records in self._paragraphs.items():
            raw_xml = self._raw_parts[part_name]
            replacements = {
                records[index].raw_index: text
                for (name, index), text in self._replacements.items()
                if name == part_name and index < len(records)
            }
            if replacements:
                patched = _patch_paragraphs(raw_xml, replacements)
                (self._tmpdir / part_name).write_text(patched, encoding="utf-8")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in self._tmpdir.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, file_path.relative_to(self._tmpdir).as_posix())

    def _word_xml_parts(self) -> list[str]:
        parts = ["word/document.xml"]
        word_dir = self._tmpdir / "word"
        if word_dir.exists():
            for pattern in ("header*.xml", "footer*.xml", "footnotes.xml", "endnotes.xml"):
                parts.extend(f"word/{path.name}" for path in sorted(word_dir.glob(pattern)))
        return [part for part in parts if (self._tmpdir / part).exists()]

    def _collect_paragraphs(self, root: ET.Element, raw_xml: str) -> list[_ParagraphRecord]:
        records: list[_ParagraphRecord] = []
        raw_para_count = len(_paragraph_matches(raw_xml))
        for raw_index, paragraph in enumerate(root.iter(_w("p"))):
            text_nodes: list[ET.Element] = []
            _collect_visible_text_nodes(paragraph, text_nodes, skip=False)
            if text_nodes:
                records.append(
                    _ParagraphRecord(
                        paragraph=paragraph,
                        text_nodes=text_nodes,
                        raw_index=raw_index if raw_index < raw_para_count else len(records),
                    )
                )
        return records


def _collect_visible_text_nodes(node: ET.Element, out: list[ET.Element], skip: bool) -> None:
    local = _local_name(node.tag)
    skip = skip or local in SKIP_ANCESTOR_LOCAL_NAMES
    if local == "instrText":
        return
    if local == "t" and not skip:
        out.append(node)
        return
    for child in list(node):
        _collect_visible_text_nodes(child, out, skip=skip)


PARAGRAPH_RE = re.compile(r"<w:p(?:\s[^>]*)?>.*?</w:p>", re.DOTALL)
TEXT_RE = re.compile(r"(<w:t\b[^>]*>)(.*?)(</w:t>)", re.DOTALL)
PROTECTED_FRAGMENT_RE = re.compile(
    r"<(?:m:oMath|m:oMathPara|w:drawing|w:pict|w:object|w:fldSimple|w:instrText|w:sdt)\b"
)


def _paragraph_matches(raw_xml: str) -> list[re.Match[str]]:
    return list(PARAGRAPH_RE.finditer(raw_xml))


def _patch_paragraphs(raw_xml: str, replacements: dict[int, str]) -> str:
    matches = _paragraph_matches(raw_xml)
    chunks: list[str] = []
    cursor = 0
    for idx, match in enumerate(matches):
        chunks.append(raw_xml[cursor : match.start()])
        paragraph_xml = match.group(0)
        if idx in replacements:
            paragraph_xml = _patch_paragraph_text(paragraph_xml, replacements[idx])
        chunks.append(paragraph_xml)
        cursor = match.end()
    chunks.append(raw_xml[cursor:])
    return "".join(chunks)


def _patch_paragraph_text(paragraph_xml: str, new_text: str) -> str:
    if PROTECTED_FRAGMENT_RE.search(paragraph_xml):
        return paragraph_xml

    text_matches = list(TEXT_RE.finditer(paragraph_xml))
    if not text_matches:
        return paragraph_xml

    original_texts = [html.unescape(match.group(2)) for match in text_matches]
    lengths = [len(text) for text in original_texts]
    if sum(lengths) == 0:
        return paragraph_xml

    pieces: list[str] = []
    cursor = 0
    for idx, length in enumerate(lengths):
        if idx == len(lengths) - 1:
            piece = new_text[cursor:]
        else:
            piece = new_text[cursor : cursor + length]
            cursor += length
        pieces.append(_xml_escape(piece))

    chunks: list[str] = []
    cursor = 0
    for match, piece in zip(text_matches, pieces):
        chunks.append(paragraph_xml[cursor : match.start()])
        open_tag = _ensure_space_preserve(match.group(1), html.unescape(piece))
        chunks.append(open_tag)
        chunks.append(piece)
        chunks.append(match.group(3))
        cursor = match.end()
    chunks.append(paragraph_xml[cursor:])
    return "".join(chunks)


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _ensure_space_preserve(open_tag: str, text: str) -> str:
    if not (text.startswith(" ") or text.endswith(" ")):
        return open_tag
    if "xml:space=" in open_tag:
        return open_tag
    return open_tag[:-1] + ' xml:space="preserve">'
