"""T3.1 通用任务的既定类型字段校验与只读队列查询。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from intellect.core.errors import TaskExecutionError


@dataclass(frozen=True)
class TaskRecord:
    id: int
    task_type: int
    status: int
    retry_count: int
    source_document_id: int | None
    chunk_batch: int | None
    parse_config: str | None
    chunk_count: int | None
    extract_config: str | None
    knowledge_id: int | None
    version_id: int | None
    operation: int | None
    error_info: str | None


def _integer(value, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise TaskExecutionError(f"任务字段 {name} 必须为不小于 {minimum} 的整数")


def _json_object(value, name: str) -> None:
    try:
        if not isinstance(value, str) or not isinstance(json.loads(value), dict):
            raise ValueError("不是 JSON 对象")
    except (ValueError, TypeError) as exc:
        raise TaskExecutionError(f"任务字段 {name} 必须为 JSON 对象快照") from exc


@contextmanager
def _read_snapshot(connection: sqlite3.Connection):
    name = "task_read_" + uuid4().hex
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
        connection.execute(f"RELEASE SAVEPOINT {name}")
    except BaseException:
        connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
        connection.execute(f"RELEASE SAVEPOINT {name}")
        raise


class TaskQueryService:
    """查询不会领取任务、刷新心跳或修改任何任务/业务记录。"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get(self, task_id: int) -> TaskRecord:
        _integer(task_id, "id", 1)
        with _read_snapshot(self.connection):
            row = self.connection.execute(
                "SELECT id,task_type,status,retry_count,source_document_id,chunk_batch,parse_config,"
                "chunk_count,extract_config,knowledge_id,version_id,operation,error_info "
                "FROM task_process WHERE id=?", (task_id,),
            ).fetchone()
            if row is None:
                raise TaskExecutionError("任务不存在", task_id=task_id)
            task = TaskRecord(*row)
            self._validate(task)
            return task

    def list_pending(self, task_type: int) -> tuple[TaskRecord, ...]:
        _integer(task_type, "task_type")
        if task_type not in (0, 1):
            raise TaskExecutionError("任务类型只能为导入或索引同步")
        with _read_snapshot(self.connection):
            rows = self.connection.execute(
                "SELECT id FROM task_process WHERE task_type=? AND status=0 ORDER BY id", (task_type,),
            ).fetchall()
            return tuple(self.get(row[0]) for row in rows)

    def _validate(self, task: TaskRecord) -> None:
        _integer(task.task_type, "task_type")
        _integer(task.status, "status")
        _integer(task.retry_count, "retry_count")
        if task.task_type not in (0, 1) or task.status not in (0, 1, 2, 3, 4):
            raise TaskExecutionError("未知任务类型或状态", task_id=task.id)
        if task.task_type == 0:
            _integer(task.source_document_id, "source_document_id", 1)
            _integer(task.chunk_batch, "chunk_batch", 1)
            _json_object(task.parse_config, "parse_config")
            if task.chunk_count is not None:
                _integer(task.chunk_count, "chunk_count")
            if task.extract_config is not None:
                _json_object(task.extract_config, "extract_config")
            if any(value is not None for value in (task.knowledge_id, task.version_id, task.operation)):
                raise TaskExecutionError("导入任务不能携带索引同步字段", task_id=task.id)
            if self.connection.execute("SELECT 1 FROM source_document WHERE id=?", (task.source_document_id,)).fetchone() is None:
                raise TaskExecutionError("任务来源文档不存在", task_id=task.id)
        else:
            if task.status == 2:
                raise TaskExecutionError("索引同步任务不能使用待审核状态", task_id=task.id)
            if any(value is not None for value in (task.source_document_id, task.chunk_batch, task.parse_config,
                                                   task.chunk_count, task.extract_config)):
                raise TaskExecutionError("索引同步任务不能携带文档导入字段", task_id=task.id)
            _integer(task.knowledge_id, "knowledge_id", 1)
            _integer(task.version_id, "version_id", 1)
            _integer(task.operation, "operation")
            if task.operation not in (0, 1):
                raise TaskExecutionError("索引操作只能为更新或删除", task_id=task.id)
            row = self.connection.execute("SELECT knowledge_id FROM knowledge_version WHERE id=?", (task.version_id,)).fetchone()
            if row is None or row[0] != task.knowledge_id:
                raise TaskExecutionError("同步目标版本不属于目标知识", task_id=task.id)
