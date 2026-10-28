"""T3.1 有限次任务轮询；不包含自动重试、超时恢复或默认向量消费者。"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from intellect.core.errors import TaskExecutionError, build_error_info
from intellect.core.tasks import (
    DatabaseTaskErrorRecorder, TaskLease, TaskOwnershipError, TaskRecord, TaskStateService, task_transaction,
)
from intellect.doc.processing import DocumentProcessingService
from intellect.extract.model import LangChainExtractor
from intellect.extract.service import ExtractionService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskContext:
    connection: sqlite3.Connection
    lease: TaskLease

    def heartbeat(self) -> None:
        TaskStateService(self.connection).heartbeat(self.lease)

    def transaction(self):
        return task_transaction(self.connection, self.lease)


class ImportTaskHandler:
    """接入解析/抽取；匹配审核由后续服务注入，未判定的候选不能虚假完成。"""

    def __init__(self, extractor: LangChainExtractor, *, match_and_review: Callable[[TaskContext], None] | None = None):
        if not isinstance(extractor, LangChainExtractor) or (match_and_review is not None and not callable(match_and_review)):
            raise TaskExecutionError("必须提供抽取适配器和可调用的匹配审核步骤")
        self.extractor, self.match_and_review = extractor, match_and_review

    def __call__(self, context: TaskContext) -> None:
        if context.lease.task_type != 0:
            raise TaskExecutionError("导入处理器不能处理同步任务")
        context.heartbeat()
        DocumentProcessingService(context.connection, lease=context.lease).run(context.lease.task_id)
        context.heartbeat()
        ExtractionService(context.connection, self.extractor, lease=context.lease).run(context.lease.task_id)
        if self.match_and_review is not None:
            self.match_and_review(context)
        # 是否完整、应待审核或完成由状态服务根据实际结果复核，不能只看模型返回。


class TaskWorker:
    def __init__(self, connection: sqlite3.Connection, handlers: Mapping[int, Callable[[TaskContext], None]]):
        self.connection = connection
        self.handlers = dict(handlers)
        if any(type(kind) is not int or kind not in (0, 1) or not callable(handler)
               for kind, handler in self.handlers.items()):
            raise TaskExecutionError("worker 只接受已注册的导入/同步可调用处理器")
        self.states = TaskStateService(connection)

    def run_once(self, task_type: int = 0) -> TaskRecord | None:
        if self.connection.in_transaction:
            raise TaskExecutionError("worker 不能在外层事务内轮询或执行模型")
        if type(task_type) is not int or task_type not in (0, 1):
            raise TaskExecutionError("未知任务类型")
        if task_type not in self.handlers:
            return None
        lease = self.states.claim(task_type)
        if lease is None:
            return None
        try:
            self.handlers[task_type](TaskContext(self.connection, lease))
            if self.connection.in_transaction:
                self.connection.rollback()
                raise TaskExecutionError("处理器未关闭自己的业务事务", task_id=lease.task_id)
            return self.states.finish(lease)
        except TaskOwnershipError:
            if self.connection.in_transaction:
                self.connection.rollback()
            logger.warning("忽略失效执行者 task_id=%s retry_count=%s", lease.task_id, lease.retry_count)
            raise
        except Exception as exc:
            if self.connection.in_transaction:
                self.connection.rollback()
            recorder = DatabaseTaskErrorRecorder(self.connection, lease)
            recorder.record_error(lease.task_id, build_error_info(exc, task_id=lease.task_id))
            logger.exception("任务执行失败 task_id=%s retry_count=%s", lease.task_id, lease.retry_count)
            return self.states.get(lease.task_id)

    def run_pending(self, task_type: int = 0, *, limit: int) -> tuple[TaskRecord, ...]:
        if type(limit) is not int or limit <= 0:
            raise TaskExecutionError("轮询次数必须为正整数")
        results = []
        for _ in range(limit):
            result = self.run_once(task_type)
            if result is None:
                break
            results.append(result)
        return tuple(results)
