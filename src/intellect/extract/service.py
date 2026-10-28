"""T2.4：固定任务抽取配置、逐切片原子结果和成功重试复用。"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from intellect.core.tasks import TaskLease
from intellect.doc.batches import DocumentBatchService, _atomic
from intellect.doc.hashing import chunk_hash
from intellect.extract.model import ExtractionError, LangChainExtractor, canonical_json, validate_output
from intellect.knowledge.hashing import knowledge_hash

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractionInput:
    task_id: int
    document_id: int
    chunk_batch: int
    parse_config: str
    chunk_count: int
    extract_config: str
    chunk_id: int
    chunk_index: int
    content: str
    chunk_hash: str
    token_count: int | None
    location_meta: str | None


@dataclass(frozen=True)
class Candidate:
    id: int
    title: str
    content: str
    extraction_index: int
    extract_model: str


@dataclass(frozen=True)
class ExtractionResult:
    task_id: int
    chunk_id: int
    candidates: tuple[Candidate, ...]


class ExtractionService:
    def __init__(self, connection: sqlite3.Connection, extractor: LangChainExtractor, *, lease: TaskLease | None = None):
        self.connection = connection
        self.lease = lease
        if not isinstance(extractor, LangChainExtractor):
            raise ExtractionError("抽取服务需要版本化 LangChain 适配器")
        self.extractor = extractor
        required = {"task_process": {"extract_config"}, "candidate": {"title", "extraction_index"},
                    "extraction_result": {"task_process_id", "chunk_id", "candidate_count"}}
        for table, fields in required.items():
            if not fields <= {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}:
                raise ExtractionError("抽取服务需要 V0.9 结构；已有事实库须先评审迁移")
        self.batches = DocumentBatchService(connection, lease=lease)

    def _task(self, task_id: int):
        if self.lease is not None:
            self.lease.touch(self.connection, task_id, 0)
        batch = self.batches.get(task_id)
        row = self.connection.execute(
            "SELECT d.status,t.extract_config FROM task_process t JOIN source_document d "
            "ON d.id=t.source_document_id WHERE t.id=?", (task_id,),
        ).fetchone()
        if row[0] != 0 or batch.chunk_count is None:
            raise ExtractionError("只允许抽取启用文档的完整发布批次")
        config = row[1]
        if config is not None:
            try:
                if json.loads(config) != self.extractor.manifest:
                    raise ValueError("抽取配置不一致")
            except (ValueError, TypeError) as exc:
                raise ExtractionError("抽取模型、提示词或结构版本与任务快照不一致") from exc
        return batch, config

    def prepare(self, task_id: int) -> tuple[ExtractionInput, ...]:
        """模型调用前持久化配置；仅返回完整固定批次的来源快照。"""
        with _atomic(self.connection):
            batch, config = self._task(task_id)
            rows = self.connection.execute(
                "SELECT id,chunk_index,content,chunk_hash,token_count,location_meta FROM document_chunk "
                "WHERE source_document_id=? AND chunk_batch=? ORDER BY chunk_index",
                (batch.source_document_id, batch.chunk_batch),
            ).fetchall()
            if len(rows) != batch.chunk_count or any(row[1] != index or chunk_hash(row[2]) != row[3]
                                                   for index, row in enumerate(rows)):
                raise ExtractionError("已发布批次切片不完整或哈希不一致")
            if config is None:
                for table in ("candidate", "extraction_result"):
                    if self.connection.execute(f"SELECT 1 FROM {table} WHERE task_process_id=? LIMIT 1", (task_id,)).fetchone():
                        raise ExtractionError("已有抽取记录却缺少固定抽取配置")
                config = canonical_json(self.extractor.manifest)
                self.connection.execute("UPDATE task_process SET extract_config=? WHERE id=?", (config, task_id))
            valid_ids = {row[0] for row in rows}
            for table in ("candidate", "extraction_result"):
                owners = self.connection.execute(f"SELECT chunk_id FROM {table} WHERE task_process_id=?", (task_id,))
                if any(row[0] not in valid_ids for row in owners):
                    raise ExtractionError("抽取记录的来源切片不属于任务固定批次")
            return tuple(ExtractionInput(task_id, batch.source_document_id, batch.chunk_batch,
                                         batch.parse_config, batch.chunk_count, config, *row) for row in rows)

    def _validate_source(self, source: ExtractionInput) -> None:
        batch, config = self._task(source.task_id)
        current = (batch.source_document_id, batch.chunk_batch, batch.parse_config, batch.chunk_count, config)
        expected = (source.document_id, source.chunk_batch, source.parse_config, source.chunk_count, source.extract_config)
        row = self.connection.execute(
            "SELECT chunk_index,content,chunk_hash,token_count,location_meta FROM document_chunk "
            "WHERE id=? AND source_document_id=? AND chunk_batch=?",
            (source.chunk_id, source.document_id, source.chunk_batch),
        ).fetchone()
        if (config is None or not 0 <= source.chunk_index < batch.chunk_count
                or current != expected or row != (source.chunk_index, source.content, source.chunk_hash,
                                           source.token_count, source.location_meta)
                or chunk_hash(source.content) != source.chunk_hash):
            raise ExtractionError("任务或来源切片已变化，不能提交或复用旧抽取结果")

    def _existing(self, source: ExtractionInput) -> ExtractionResult | None:
        checkpoint = self.connection.execute(
            "SELECT candidate_count FROM extraction_result WHERE task_process_id=? AND chunk_id=?",
            (source.task_id, source.chunk_id),
        ).fetchone()
        rows = self.connection.execute(
            "SELECT id,title,content,extraction_index,extract_model FROM candidate "
            "WHERE task_process_id=? AND chunk_id=? ORDER BY extraction_index,id",
            (source.task_id, source.chunk_id),
        ).fetchall()
        if checkpoint is None:
            if rows:
                raise ExtractionError("候选存在却缺少成功检查点，不能推断完成或覆盖")
            return None
        if type(checkpoint[0]) is not int or checkpoint[0] != len(rows):
            raise ExtractionError("成功检查点候选数量不完整")
        model = canonical_json(self.extractor.manifest["model"])
        for index, row in enumerate(rows):
            if row[3] != index or row[4] != model:
                raise ExtractionError("已有候选序号或模型版本不一致")
            validate_output({"items": [{"title": row[1], "content": row[2]}]})
        return ExtractionResult(source.task_id, source.chunk_id, tuple(Candidate(*row) for row in rows))

    def save_result(self, source: ExtractionInput, output) -> ExtractionResult:
        """模型返回后的独立提交入口；支持外层事务，不触发模型调用。"""
        output = validate_output(output)
        for item in output.items:
            knowledge_hash(item.content)  # 在首次持久化候选前验收固定业务哈希，不额外存哈希列
        with _atomic(self.connection):
            self._validate_source(source)
            existing = self._existing(source)
            if existing is not None:
                return existing  # 并发中先成功的完整结果获保留
            model = canonical_json(self.extractor.manifest["model"])
            for index, item in enumerate(output.items):
                self.connection.execute(
                    "INSERT INTO candidate (title,content,task_process_id,chunk_id,extraction_index,extract_model) "
                    "VALUES (?,?,?,?,?,?)", (item.title, item.content, source.task_id, source.chunk_id, index, model),
                )
            self.connection.execute(
                "INSERT INTO extraction_result (task_process_id,chunk_id,candidate_count) VALUES (?,?,?)",
                (source.task_id, source.chunk_id, len(output.items)),
            )
            result = self._existing(source)
        logger.info("切片抽取成功 task_id=%s chunk_id=%s candidate_count=%s",
                    source.task_id, source.chunk_id, len(result.candidates))
        return result

    def run(self, task_id: int) -> tuple[ExtractionResult, ...]:
        results = []
        for source in self.prepare(task_id):
            with _atomic(self.connection):
                self._validate_source(source)
                existing = self._existing(source)
            if existing is None:
                if self.connection.in_transaction:
                    raise ExtractionError("模型调用须在调用方写事务之外；可先 prepare，再在事务内 save_result")
                output = self.extractor.invoke(source.content)
                existing = self.save_result(source, output)
            results.append(existing)
        return tuple(results)
