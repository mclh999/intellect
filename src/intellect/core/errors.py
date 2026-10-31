"""统一错误处理基线（T0.4）。

约定：
- 业务异常一律继承 :class:`IntellectError`，携带稳定 ``code`` 与结构化上下文
  ``context``，便于日志与错误信息统一呈现；
- 后台任务（``task_process``）执行失败的错误，经 :func:`build_error_info`
  序列化为统一 JSON 文本写入 ``task_process.error_info``（TEXT 列）——该列是
  任务错误唯一归宿，禁止各 worker 自行拼格式；
- :class:`TaskErrorRecorder` 是 error_info 落库通道接口，由 T3.1 任务 worker
  以数据库实现（与任务状态更新同一事务提交），业务代码不直接写 SQL。
"""

from __future__ import annotations

import datetime as dt
import json
import traceback
from typing import Any, Protocol, runtime_checkable

# 序列化进 error_info 的 traceback 最大行数；超出保留尾部（最内层帧与异常行，
# 即根因端），避免 TEXT 列被深递归堆栈撑爆。
TRACEBACK_MAX_LINES = 30


class IntellectError(Exception):
    """业务异常基类：稳定错误码 + 结构化上下文。"""

    code: str = "INTELLECT_ERROR"

    def __init__(self, message: str, *, context: dict[str, Any] | None = None):
        """实现 `__init__` 生命周期钩子，初始化或维护对象状态。 参数包括：`message`、`context`。"""
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context or {})


class TaskExecutionError(IntellectError):
    """任务执行失败（task_process 0 文档导入 / 1 索引同步 通用）。"""

    code = "TASK_EXECUTION_FAILED"

    def __init__(
        self,
        message: str,
        *,
        task_id: int | None = None,
        task_type: int | None = None,
        context: dict[str, Any] | None = None,
    ):
        """实现 `__init__` 生命周期钩子，初始化或维护对象状态。 参数包括：`message`、`task_id`、`task_type`、`context`。"""
        merged = dict(context or {})
        if task_id is not None:
            merged["task_id"] = task_id
        if task_type is not None:
            merged["task_type"] = task_type
        super().__init__(message, context=merged)


@runtime_checkable
class TaskErrorRecorder(Protocol):
    """error_info 落库通道接口（T3.1 以数据库实现）。

    实现方须保证：error_info 写入与任务状态置"已失败"在同一事务内完成。
    """

    """记录任务失败信息，并由实现方确保与失败状态在同一事务写入。"""
    def record_error(self, task_id: int, error_info: str) -> None: ...


def build_error_info(exc: BaseException, *, task_id: int | None = None) -> str:
    """将异常序列化为 task_process.error_info 的统一 JSON 文本。"""
    formatted = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    ).rstrip("\n")
    lines = formatted.splitlines()
    if len(lines) > TRACEBACK_MAX_LINES:
        lines = [f"...（共 {len(lines)} 行，保留尾部 {TRACEBACK_MAX_LINES} 行）"] + lines[
            -TRACEBACK_MAX_LINES:
        ]

    payload: dict[str, Any] = {
        "code": getattr(exc, "code", type(exc).__name__),
        "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "message": str(exc),
        "context": getattr(exc, "context", None) or {},
        "task_id": task_id,
        "occurred_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "traceback": "\n".join(lines),
    }
    # 路径、日期等上下文对象保留文本表示，避免错误记录因序列化再次失败。
    return json.dumps(payload, ensure_ascii=False, default=str)
