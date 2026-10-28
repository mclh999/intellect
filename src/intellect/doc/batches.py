"""导入批次分配、切片幂等补齐与完整发布（T2.8，DESIGN V0.7 §3.3）。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from intellect.core.config import ChunkConfig, get_settings
from intellect.core.errors import IntellectError
from intellect.doc.hashing import chunk_hash


class DocumentBatchError(IntellectError):
    code = "DOCUMENT_BATCH_FAILED"


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise DocumentBatchError(f"{name} 必须为不小于 {minimum} 的整数")


def _json(value: dict) -> str:
    if not isinstance(value, dict):
        raise DocumentBatchError("配置或位置元数据必须为 JSON 对象")
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise DocumentBatchError("配置或位置元数据无法序列化为 JSON") from exc


@contextmanager
def _atomic(connection: sqlite3.Connection):
    nested = connection.in_transaction
    savepoint = "document_batch_" + uuid4().hex
    connection.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
        if nested:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            connection.commit()
    except BaseException:
        if nested:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            connection.rollback()
        raise


@dataclass(frozen=True)
class ImportBatch:
    task_id: int
    source_document_id: int
    chunk_batch: int
    parse_config: str
    chunk_count: int | None


@dataclass(frozen=True)
class ChunkInput:
    chunk_index: int
    content: str
    token_count: int | None = None
    location_meta: dict | None = None


class DocumentBatchService:
    """不调用解析器，不管理 worker 状态；每次写调用原子执行且支持外层事务。

    调用方用 get().parse_config 快照恢复解析；只有解析成功读完输入才可 publish。
    已提交片段在后续调用失败时保留；当前调用中的写入全部回滚。
    """

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        columns = {row[1] for row in connection.execute("PRAGMA table_info(task_process)")}
        if not {"chunk_batch", "parse_config", "chunk_count"} <= columns:
            raise DocumentBatchError("批次服务需要 V0.7 结构；已有事实库须先评审迁移")

    def get(self, task_id: int) -> ImportBatch:
        _integer(task_id, "task_id", 1)
        row = self.connection.execute(
            "SELECT id, source_document_id, chunk_batch, parse_config, chunk_count "
            "FROM task_process WHERE id = ? AND task_type = 0", (task_id,),
        ).fetchone()
        if row is None or any(value is None for value in row[:4]):
            raise DocumentBatchError("导入任务不存在或缺少批次快照", context={"task_id": task_id})
        return ImportBatch(*row)

    def _document(self, document_id: int):
        row = self.connection.execute(
            "SELECT chunk_batch, status FROM source_document WHERE id = ?", (document_id,),
        ).fetchone()
        if row is None or row[1] != 0:
            raise DocumentBatchError("文档不存在或已停用", context={"source_document_id": document_id})
        return row

    def create(
        self, source_document_id: int, *, parser: str, parser_version: str,
        chunking: ChunkConfig | None = None, parser_options: dict | None = None,
    ) -> ImportBatch:
        _integer(source_document_id, "source_document_id", 1)
        if any(not isinstance(value, str) or not value.strip() for value in (parser, parser_version)):
            raise DocumentBatchError("必须明确解析器及版本")
        if chunking is not None and not isinstance(chunking, ChunkConfig):
            raise DocumentBatchError("切分参数必须为 ChunkConfig")
        chunking = chunking if chunking is not None else get_settings().chunk
        snapshot = _json({
            "parser": parser, "parser_version": parser_version,
            **chunking.model_dump(), "parser_options": parser_options if parser_options is not None else {},
        })
        # 先验证嵌套 options 的对象类型，避免把非对象持久化为配置。
        if parser_options is not None and not isinstance(parser_options, dict):
            raise DocumentBatchError("parser_options 必须为 JSON 对象")
        with _atomic(self.connection):
            current, _ = self._document(source_document_id)
            pending = self.connection.execute(
                "SELECT id FROM task_process WHERE task_type = 0 AND source_document_id = ? "
                "AND chunk_count IS NULL", (source_document_id,),
            ).fetchone()
            if pending:
                raise DocumentBatchError("该文档存在未发布批次，请重试原任务", context={"task_id": pending[0]})
            maximum = self.connection.execute(
                "SELECT MAX(chunk_batch) FROM ("
                "SELECT chunk_batch FROM task_process WHERE task_type = 0 AND source_document_id = ? "
                "UNION ALL SELECT chunk_batch FROM document_chunk WHERE source_document_id = ?)",
                (source_document_id, source_document_id),
            ).fetchone()[0]
            batch = max(current or 0, maximum or 0) + 1
            task_id = self.connection.execute(
                "INSERT INTO task_process (task_type, source_document_id, chunk_batch, parse_config) "
                "VALUES (0, ?, ?, ?)", (source_document_id, batch, snapshot),
            ).lastrowid
            return self.get(task_id)

    def save_chunks(self, task_id: int, chunks: Iterable[ChunkInput]) -> list[int]:
        ids = []
        with _atomic(self.connection):
            batch = self.get(task_id)
            self._document(batch.source_document_id)
            if batch.chunk_count is not None:
                raise DocumentBatchError("该批次已经完整发布，禁止追加或改写切片")
            for chunk in chunks:
                if not isinstance(chunk, ChunkInput):
                    raise DocumentBatchError("切片输入必须为 ChunkInput")
                _integer(chunk.chunk_index, "chunk_index")
                if chunk.token_count is not None:
                    _integer(chunk.token_count, "token_count")
                digest = chunk_hash(chunk.content)
                location = _json(chunk.location_meta) if chunk.location_meta is not None else None
                existing = self.connection.execute(
                    "SELECT id, content, chunk_hash, token_count, location_meta FROM document_chunk "
                    "WHERE source_document_id = ? AND chunk_batch = ? AND chunk_index = ?",
                    (batch.source_document_id, batch.chunk_batch, chunk.chunk_index),
                ).fetchone()
                if existing:
                    if tuple(existing[1:]) != (chunk.content, digest, chunk.token_count, location):
                        raise DocumentBatchError("已有切片与重试结果不一致", context={
                            "task_id": task_id, "chunk_index": chunk.chunk_index,
                        })
                    ids.append(existing[0])
                else:
                    ids.append(self.connection.execute(
                        "INSERT INTO document_chunk (source_document_id, chunk_batch, chunk_index, "
                        "content, chunk_hash, token_count, location_meta) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (batch.source_document_id, batch.chunk_batch, chunk.chunk_index,
                         chunk.content, digest, chunk.token_count, location),
                    ).lastrowid)
        return ids

    def publish(self, task_id: int, *, expected_count: int) -> ImportBatch:
        """expected_count 必须来自成功结束的解析过程，不能用已存行数猜测。"""
        _integer(expected_count, "expected_count")
        with _atomic(self.connection):
            batch = self.get(task_id)
            current, _ = self._document(batch.source_document_id)
            if batch.chunk_count is not None and batch.chunk_count != expected_count:
                raise DocumentBatchError("重试发布的切片数与已发布结果不一致")
            rows = self.connection.execute(
                "SELECT chunk_index, content, chunk_hash FROM document_chunk "
                "WHERE source_document_id = ? AND chunk_batch = ? ORDER BY chunk_index",
                (batch.source_document_id, batch.chunk_batch),
            )
            count = 0
            for index, content, digest in rows:
                if index != count or chunk_hash(content) != digest:
                    raise DocumentBatchError("切片存在缺口或哈希不一致，不能发布")
                count += 1
            if count != expected_count:
                raise DocumentBatchError("切片数量与解析结果不一致，不能发布")
            if batch.chunk_count is not None:
                return batch  # 重试旧任务不能把有效批次指针倒退。
            if current is not None and current >= batch.chunk_batch:
                raise DocumentBatchError("当前有效批次已前进，不能发布过期任务")
            self.connection.execute(
                "UPDATE task_process SET chunk_count = ?, update_time = datetime('now') WHERE id = ?",
                (expected_count, task_id),
            )
            self.connection.execute(
                "UPDATE source_document SET chunk_batch = ?, parse_config = ? WHERE id = ?",
                (batch.chunk_batch, batch.parse_config, batch.source_document_id),
            )
            return self.get(task_id)
