"""固定版本的 TXT/PDF/DOCX/归档 HTML 文本解析适配器。"""

from __future__ import annotations

import codecs
import io
import platform
from dataclasses import dataclass
from html.parser import HTMLParser
from importlib.metadata import version

from intellect.core.config import get_settings
from intellect.core.errors import IntellectError
from intellect.doc.hashing import document_hash, file_hash


class DocumentParseError(IntellectError):
    code = "DOCUMENT_PARSE_FAILED"


@dataclass(frozen=True)
class TextBlock:
    text: str
    start: int
    location: dict


@dataclass(frozen=True)
class ParsedDocument:
    content: str
    blocks: tuple[TextBlock, ...]


@dataclass(frozen=True)
class DocumentInspection:
    parsed: ParsedDocument
    file_hash: str
    content_hash: str
    file_size: int


def source_type(value: str) -> str:
    if not isinstance(value, str) or value.upper() not in {"TXT", "PDF", "DOCX", "WEB"}:
        raise DocumentParseError("不支持的文档类型", context={"source_type": value})
    return value.upper()


def parser_version(kind: str) -> str:
    kind = source_type(kind)
    backend = {"PDF": "pypdf", "DOCX": "python-docx"}.get(kind)
    library = f"/{backend}-{version(backend)}" if backend else ""
    if kind == "DOCX":
        library += f"/lxml-{version('lxml')}"
    return f"{kind.lower()}-v1/python-{platform.python_version()}{library}"


def default_options(kind: str) -> dict:
    return {"encoding": get_settings().document.text_encoding} if source_type(kind) in {"TXT", "WEB"} else {}


def validate_options(kind: str, options: dict) -> None:
    expected = {"encoding"} if source_type(kind) in {"TXT", "WEB"} else set()
    if not isinstance(options, dict) or set(options) != expected:
        raise DocumentParseError("解析器参数缺失或含未知字段")
    if expected:
        try:
            if not isinstance(options["encoding"], str):
                raise LookupError("编码必须为字符串")
            codecs.lookup(options["encoding"])
            "a".encode(options["encoding"])
        except (LookupError, ValueError) as exc:
            raise DocumentParseError("未知文本编码") from exc


def _assemble(parts: list[tuple[str, dict]]) -> ParsedDocument:
    blocks = []
    start = 0
    for index, (text, location) in enumerate(parts):
        text = text + ("\n" if index < len(parts) - 1 else "")
        blocks.append(TextBlock(text, start, location))
        start += len(text)
    return ParsedDocument("".join(block.text for block in blocks), tuple(blocks))


class _HtmlText(HTMLParser):
    _BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "section",
               "article", "table", "ul", "ol", "pre", "blockquote", "header", "footer", "main", "br", "hr"}
    _IGNORED = {"script", "style", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = []

    def _break(self):
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in self._IGNORED:
            self.ignored.append(tag)
        elif not self.ignored and tag in self._BLOCKS:
            self._break()

    def handle_endtag(self, tag):
        if self.ignored:
            if self.ignored[-1] == tag:
                self.ignored.pop()
        elif tag in self._BLOCKS:
            self._break()

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def _docx_parts(document) -> list[tuple[str, dict]]:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    parts = []

    def visit(container, location):
        paragraph_no = table_no = 0
        for item in container.iter_inner_content():
            if isinstance(item, Paragraph):
                paragraph_no += 1
                parts.append((item.text, {**location, "paragraph": paragraph_no}))
            elif isinstance(item, Table):
                table_no += 1
                seen_cells = set()
                for row_no, row in enumerate(item.rows, 1):
                    for cell_no, cell in enumerate(row.cells, 1):
                        if cell._tc in seen_cells:
                            continue
                        seen_cells.add(cell._tc)
                        visit(cell, {**location, "table_path": [*location.get("table_path", []),
                                                              [table_no, row_no, cell_no]]})

    visit(document, {"format": "DOCX"})
    return parts


def parse_bytes(content: bytes, kind: str, *, options: dict | None = None) -> ParsedDocument:
    kind = source_type(kind)
    options = options if options is not None else default_options(kind)
    validate_options(kind, options)
    if not isinstance(content, bytes):
        raise DocumentParseError("解析输入必须是归档原始字节")
    try:
        if kind == "TXT":
            text = content.decode(options["encoding"], errors="strict")
            return _assemble([(text, {"format": "TXT"})])
        if kind == "WEB":
            parser = _HtmlText()
            parser.feed(content.decode(options["encoding"], errors="strict"))
            parser.close()
            return _assemble([("".join(parser.parts), {"format": "WEB"})])
        if kind == "DOCX":
            from docx import Document
            return _assemble(_docx_parts(Document(io.BytesIO(content))))
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise DocumentParseError("加密 PDF 需要先提供可解析的解密归档")
        parts = []
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text(extraction_mode="plain") or ""
            if not text.strip() and len(page.images):
                raise DocumentParseError("PDF 图像页没有文本层；当前解析器未接入 OCR", context={"page": number})
            parts.append((text, {"format": "PDF", "page": number}))
        return _assemble(parts)
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError("文档解析失败", context={"source_type": kind}) from exc


def inspect_bytes(content: bytes, kind: str, *, options: dict | None = None) -> DocumentInspection:
    """供上游登记/去重使用；本函数不新增文档记录或改写登记哈希。"""
    parsed = parse_bytes(content, kind, options=options)
    return DocumentInspection(parsed, file_hash(content), document_hash(parsed.content), len(content))
