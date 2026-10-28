"""将固定解析器与切分器接入不可变任务批次；不承担文档登记、审核或 worker。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from pydantic import ValidationError

from intellect.core.config import ChunkConfig
from intellect.core.errors import IntellectError
from intellect.doc.batches import DocumentBatchService, ImportBatch, _atomic
from intellect.doc.counting import get_counter
from intellect.doc.hashing import file_hash
from intellect.doc.parsers import default_options, parse_bytes, parser_version, source_type, validate_options
from intellect.doc.splitting import SPLITTER_VERSION, split_document


class DocumentProcessingError(IntellectError):
    code = "DOCUMENT_PROCESSING_FAILED"


def archive_path(value: str) -> Path:
    if value.startswith("file:"):
        parsed = urlparse(value)
        if parsed.netloc not in {"", "localhost"}:
            raise DocumentProcessingError("file URL 必须指向本地归档")
        value = url2pathname(parsed.path)
    elif value.startswith(("http:", "https:")):
        raise DocumentProcessingError("WEB 必须先归档为本地内容，重试不能重新抓取网址")
    return Path(value).resolve()


def _read_archive(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DocumentProcessingError("无法读取原始归档文件", context={"path": str(path)}) from exc


class DocumentProcessingService:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.batches = DocumentBatchService(connection)

    def create(
        self, document_id: int, *, chunking: ChunkConfig | None = None, parser_options: dict | None = None,
    ) -> ImportBatch:
        row = self.connection.execute(
            "SELECT storage_url, source_type, file_hash, status FROM source_document WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None or row[3] != 0:
            raise DocumentProcessingError("文档不存在或已停用")
        path = archive_path(row[0])
        inferred = {".txt": "TXT", ".pdf": "PDF", ".docx": "DOCX", ".html": "WEB", ".htm": "WEB"}.get(path.suffix.lower())
        kind = source_type(row[1] or inferred)
        options = default_options(kind)
        if parser_options is not None:
            if not isinstance(parser_options, dict):
                raise DocumentProcessingError("解析器参数必须为对象")
            options.update(parser_options)
        validate_options(kind, options)
        digest = file_hash(_read_archive(path))
        if row[2] is not None and row[2] != digest:
            raise DocumentProcessingError("归档文件与登记的原文件哈希不一致")
        snapshot = {
            "archive": path.as_uri(), "file_hash": digest, "format_options": options,
            "splitter_version": SPLITTER_VERSION, "token_counter": get_counter().manifest,
        }
        with _atomic(self.connection):
            return self.batches.create(document_id, parser=kind, parser_version=parser_version(kind),
                                       chunking=chunking, parser_options=snapshot)

    def run(self, task_id: int) -> ImportBatch:
        batch = self.batches.get(task_id)
        if batch.chunk_count is not None:
            return self.batches.publish(task_id, expected_count=batch.chunk_count)
        try:
            snapshot = json.loads(batch.parse_config)
            if set(snapshot) != {"parser", "parser_version", "chunk_size", "chunk_overlap", "separators", "parser_options"}:
                raise ValueError("配置快照字段不完整")
            options = snapshot["parser_options"]
            if set(options) != {"archive", "file_hash", "format_options", "splitter_version", "token_counter"}:
                raise ValueError("解析选项快照字段不完整")
            kind = source_type(snapshot["parser"])
            if (snapshot["parser_version"] != parser_version(kind)
                    or options["splitter_version"] != SPLITTER_VERSION
                    or options["token_counter"] != get_counter().manifest):
                raise ValueError("解析器、切分器或统计器版本与任务快照不一致")
            validate_options(kind, options["format_options"])
            chunking = ChunkConfig(**{key: snapshot[key] for key in ("chunk_size", "chunk_overlap", "separators")})
            path = archive_path(options["archive"])
        except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as exc:
            raise DocumentProcessingError("无法按不可变任务快照恢复解析") from exc
        data = _read_archive(path)
        if file_hash(data) != options["file_hash"]:
            raise DocumentProcessingError("原始归档字节已改变，不能继续旧批次")
        document = parse_bytes(data, kind, options=options["format_options"])
        count = 0
        for chunk in split_document(document, chunking, get_counter()):
            self.batches.save_chunks(task_id, [chunk])
            count += 1
        return self.batches.publish(task_id, expected_count=count)
