"""文件级去重及文档/初始批次登记（T3.2，DESIGN §5.1）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from intellect.core.config import ChunkConfig
from intellect.core.errors import IntellectError
from intellect.doc.batches import ImportBatch, _atomic
from intellect.doc.parsers import default_options, inspect_bytes, source_type, validate_options
from intellect.doc.processing import DocumentProcessingService, _read_archive, archive_path


class DocumentImportError(IntellectError):
    code = "DOCUMENT_IMPORT_FAILED"


class DuplicateDocumentError(DocumentImportError):
    code = "DUPLICATE_DOCUMENT"


@dataclass(frozen=True)
class DocumentMatch:
    document_id: int
    title: str
    storage_url: str
    same_file: bool


@dataclass(frozen=True)
class RegisteredImport:
    document_id: int
    batch: ImportBatch
    duplicate_matches: tuple[DocumentMatch, ...]


class DocumentImportService:
    def __init__(self, connection: sqlite3.Connection):
        """实现 `__init__` 生命周期钩子，初始化或维护对象状态。 参数包括：`connection`。"""
        self.connection = connection
        self.processing = DocumentProcessingService(connection)

    def register(
        self, archive: str | Path, *, title: str, kind: str,
        confirmed_duplicate: bool = False, parser_options: dict | None = None,
        chunking: ChunkConfig | None = None, uploader: str | None = None,
    ) -> RegisteredImport:
        """执行 `register` 对应的功能并返回处理结果。 参数包括：`archive`、`title`、`kind`、`confirmed_duplicate`、`parser_options`、`chunking`、`uploader`。"""
        if not isinstance(title, str) or not title.strip():
            raise DocumentImportError("必须提供文档标题")
        if type(confirmed_duplicate) is not bool:
            raise DocumentImportError("重复来源确认必须为布尔值")
        kind = source_type(kind)
        path = archive_path(str(archive))
        options = default_options(kind)
        if parser_options is not None:
            if not isinstance(parser_options, dict):
                raise DocumentImportError("解析器参数必须为对象")
            options.update(parser_options)
        validate_options(kind, options)
        # 解析先完成，再进入去重/登记事务，避免解析期间占用数据库写锁。
        inspection = inspect_bytes(_read_archive(path), kind, options=options)
        with _atomic(self.connection):
            matches = tuple(DocumentMatch(row[0], row[1], row[2], row[3] == inspection.file_hash)
                            for row in self.connection.execute(
                "SELECT id, title, storage_url, file_hash FROM source_document "
                "WHERE status = 0 AND content_hash = ? ORDER BY id", (inspection.content_hash,)
            ))
            if matches and not confirmed_duplicate:
                raise DuplicateDocumentError("文档已存在；须显式确认后才能作为独立来源导入", context={
                    "match": "file" if any(match.same_file for match in matches) else "content",
                    "documents": [{"id": match.document_id, "title": match.title,
                                   "storage_url": match.storage_url, "same_file": match.same_file} for match in matches],
                })
            document_id = self.connection.execute(
                "INSERT INTO source_document (title, storage_url, source_type, file_hash, content_hash, "
                "file_size, uploader_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title, path.as_uri(), kind, inspection.file_hash, inspection.content_hash,
                 inspection.file_size, uploader),
            ).lastrowid
            # create 再核验归档字节，若登记后准备批次失败，文档与任务全部回滚。
            batch = self.processing.create(document_id, parser_options=options, chunking=chunking)
            return RegisteredImport(document_id, batch, matches)
