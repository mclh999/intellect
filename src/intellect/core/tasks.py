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


class TaskOwnershipError(TaskExecutionError):
    code = "TASK_OWNERSHIP_LOST"


@dataclass(frozen=True)
class TaskLease:
    task_id: int
    task_type: int
    retry_count: int

    def __post_init__(self):
        _integer(self.task_id, "id", 1)
        _integer(self.task_type, "task_type")
        _integer(self.retry_count, "retry_count")
        if self.task_type not in (0, 1):
            raise TaskExecutionError("未知任务类型")

    def check(self, connection: sqlite3.Connection, task_id: int, task_type: int) -> None:
        """必须在实际提交事务内调用；retry_count 随每次失败单调增长。"""
        if not connection.in_transaction:
            raise TaskOwnershipError("所有权核验必须位于提交事务中", task_id=task_id)
        row = connection.execute(
            "SELECT task_type,status,retry_count FROM task_process WHERE id=?", (task_id,),
        ).fetchone()
        if (self.task_id != task_id or self.task_type != task_type
                or row != (self.task_type, 1, self.retry_count)):
            raise TaskOwnershipError("任务执行所有权已失效", task_id=task_id)

    def touch(self, connection: sqlite3.Connection, task_id: int, task_type: int) -> None:
        self.check(connection, task_id, task_type)
        connection.execute("UPDATE task_process SET update_time=datetime('now') WHERE id=?", (task_id,))


@contextmanager
def task_transaction(connection: sqlite3.Connection, lease: TaskLease):
    """业务处理器的短写事务；核验所有权且不提交调用方外层事务。"""
    with _write_transaction(connection):
        lease.check(connection, lease.task_id, lease.task_type)
        yield
        lease.touch(connection, lease.task_id, lease.task_type)


@contextmanager
def _write_transaction(connection: sqlite3.Connection):
    nested = connection.in_transaction
    name = "task_state_" + uuid4().hex
    connection.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
        connection.execute(f"RELEASE SAVEPOINT {name}") if nested else connection.commit()
    except BaseException:
        if nested:
            connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            connection.execute(f"RELEASE SAVEPOINT {name}")
        else:
            connection.rollback()
        raise


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


class TaskStateService(TaskQueryService):
    def claim(self, task_type: int, *, task_id: int | None = None) -> TaskLease | None:
        _integer(task_type, "task_type")
        if task_type not in (0, 1):
            raise TaskExecutionError("未知任务类型")
        if task_id is not None:
            _integer(task_id, "id", 1)
        with _write_transaction(self.connection):
            row = self.connection.execute(
                "SELECT id FROM task_process WHERE task_type=? AND status=0 "
                "AND (? IS NULL OR id=?) ORDER BY id LIMIT 1", (task_type, task_id, task_id),
            ).fetchone()
            if row is None:
                return None
            task = self.get(row[0])
            cursor = self.connection.execute(
                "UPDATE task_process SET status=1,start_time=datetime('now'),finish_time=NULL,"
                "update_time=datetime('now') WHERE id=? AND task_type=? AND status=0 AND retry_count=?",
                (task.id, task.task_type, task.retry_count),
            )
            if cursor.rowcount != 1:
                raise TaskOwnershipError("任务已被其他执行者领取", task_id=task.id)
            return TaskLease(task.id, task.task_type, task.retry_count)

    def heartbeat(self, lease: TaskLease) -> None:
        with task_transaction(self.connection, lease):
            pass

    def retry(self, task_id: int) -> TaskRecord:
        with _write_transaction(self.connection):
            task = self.get(task_id)
            if task.status != 4:
                raise TaskExecutionError("只能显式重试已失败任务", task_id=task_id)
            self.connection.execute(
                "UPDATE task_process SET status=0,start_time=NULL,finish_time=NULL,update_time=datetime('now') "
                "WHERE id=? AND status=4 AND retry_count=?", (task_id, task.retry_count),
            )
            return self.get(task_id)

    def finish(self, lease: TaskLease) -> TaskRecord:
        with _write_transaction(self.connection):
            lease.check(self.connection, lease.task_id, lease.task_type)
            task = self.get(lease.task_id)
            status = self._import_status(task) if task.task_type == 0 else 3
            self.connection.execute(
                "UPDATE task_process SET status=?,finish_time=datetime('now'),update_time=datetime('now') WHERE id=?",
                (status, task.id),
            )
            return self.get(task.id)

    def _import_status(self, task: TaskRecord) -> int:
        from intellect.doc.hashing import chunk_hash
        from intellect.extract.model import ExtractionError, extraction_manifest

        if task.chunk_count is None or task.extract_config is None:
            raise TaskExecutionError("解析或抽取尚未完成", task_id=task.id)
        config = json.loads(task.extract_config)
        identity = config.get("model")
        try:
            expected = extraction_manifest(identity)
        except ExtractionError as exc:
            raise TaskExecutionError("抽取配置缺少实际模型版本", task_id=task.id) from exc
        if config != expected:
            raise TaskExecutionError("抽取配置版本或指纹不完整", task_id=task.id)
        model = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        chunks = self.connection.execute(
            "SELECT id,chunk_index,content,chunk_hash FROM document_chunk "
            "WHERE source_document_id=? AND chunk_batch=? ORDER BY chunk_index", (task.source_document_id, task.chunk_batch),
        ).fetchall()
        checkpoints = dict(self.connection.execute(
            "SELECT chunk_id,candidate_count FROM extraction_result WHERE task_process_id=?", (task.id,),
        ))
        if (len(chunks) != task.chunk_count or set(checkpoints) != {row[0] for row in chunks}
                or any(row[1] != i or chunk_hash(row[2]) != row[3] for i, row in enumerate(chunks))):
            raise TaskExecutionError("切片或抽取成功检查点不完整", task_id=task.id)
        candidates = self.connection.execute(
            "SELECT id,chunk_id,extraction_index,extract_model,title,content,status "
            "FROM candidate WHERE task_process_id=? ORDER BY chunk_id,extraction_index,id", (task.id,),
        ).fetchall()
        groups = {}
        for row in candidates:
            groups.setdefault(row[1], []).append(row)
        for chunk_id, count in checkpoints.items():
            group = groups.get(chunk_id, [])
            if (type(count) is not int or count != len(group)
                    or any(row[2] != index or row[3] != model for index, row in enumerate(group))):
                raise TaskExecutionError("检查点候选数量、序号或模型不一致", task_id=task.id)
        if any(row[1] not in checkpoints or row[6] not in (0, 1, 2)
               or any(not isinstance(value, str) or not value.strip() for value in row[4:6]) for row in candidates):
            raise TaskExecutionError("候选归属、内容或状态不完整", task_id=task.id)
        issues = self.connection.execute(
            "SELECT r.candidate_id,r.status FROM review_issue r JOIN candidate c ON c.id=r.candidate_id "
            "WHERE c.task_process_id=?", (task.id,),
        ).fetchall()
        if any(status not in (0, 1, 2) for _, status in issues):
            raise TaskExecutionError("审核事项状态非法", task_id=task.id)
        pending = {candidate for candidate, status in issues if status == 0}
        awaiting = {row[0] for row in candidates if row[6] == 1}
        if awaiting != pending:
            raise TaskExecutionError("候选尚未完成匹配审核判定，不能结束导入", task_id=task.id)
        return 2 if pending else 3


class DatabaseTaskErrorRecorder:
    """实现 TaskErrorRecorder；绑定一次领取，错误与失败状态整体提交。"""

    def __init__(self, connection: sqlite3.Connection, lease: TaskLease):
        self.connection, self.lease = connection, lease

    def record_error(self, task_id: int, error_info: str) -> None:
        try:
            info = json.loads(error_info)
            required = {"code", "type", "message", "context", "task_id", "occurred_at", "traceback"}
            if (not isinstance(info, dict) or not required <= set(info) or info["task_id"] not in (None, task_id)
                    or any(not isinstance(info[key], str) for key in ("code", "type", "message", "occurred_at", "traceback"))
                    or not isinstance(info["context"], dict)):
                raise ValueError("不符合统一错误格式")
        except (ValueError, TypeError) as exc:
            raise TaskExecutionError("必须通过 build_error_info 提供统一任务错误", task_id=task_id) from exc
        with _write_transaction(self.connection):
            self.lease.check(self.connection, task_id, self.lease.task_type)
            self.connection.execute(
                "UPDATE task_process SET status=4,error_info=?,retry_count=retry_count+1,"
                "finish_time=datetime('now'),update_time=datetime('now') WHERE id=?", (error_info, task_id),
            )
