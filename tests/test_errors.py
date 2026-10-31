"""T0.4 错误处理基线测试：异常基类、error_info 序列化与落库通道接口。"""

from __future__ import annotations

import json
from pathlib import Path

from intellect.core.errors import (
    IntellectError,
    TaskErrorRecorder,
    TaskExecutionError,
    build_error_info,
)


class TestIntellectError:
    def test_code_and_context(self):
        """验证 `code_and_context` 所代表的行为。 参数包括：无显式参数。"""
        err = IntellectError("解析失败", context={"doc_id": 7})
        assert err.code == "INTELLECT_ERROR"
        assert err.message == "解析失败"
        assert err.context == {"doc_id": 7}

    def test_task_error_carries_task_context(self):
        """验证 `task_error_carries_task_context` 所代表的行为。 参数包括：无显式参数。"""
        err = TaskExecutionError("嵌入超时", task_id=42, task_type=1)
        assert err.code == "TASK_EXECUTION_FAILED"
        assert err.context == {"task_id": 42, "task_type": 1}


class TestBuildErrorInfo:
    def test_non_json_context_keeps_error_recordable(self):
        """验证 `non_json_context_keeps_error_recordable` 所代表的行为。 参数包括：无显式参数。"""
        info = json.loads(build_error_info(IntellectError(
            "文件读取失败", context={"path": Path("source/document.txt")}
        )))
        assert info["message"] == "文件读取失败"
        assert info["context"]["path"] == str(Path("source/document.txt"))

    def test_payload_fields(self):
        """验证 `payload_fields` 所代表的行为。 参数包括：无显式参数。"""
        try:
            raise TaskExecutionError("嵌入超时", task_id=42, task_type=1)
        except TaskExecutionError as exc:
            info = json.loads(build_error_info(exc, task_id=42))
        assert info["code"] == "TASK_EXECUTION_FAILED"
        assert info["message"] == "嵌入超时"
        assert info["task_id"] == 42
        assert info["context"] == {"task_id": 42, "task_type": 1}
        assert info["occurred_at"]
        assert "test_errors.py" in info["traceback"]

    def test_plain_exception_uses_type_name_as_code(self):
        """验证 `plain_exception_uses_type_name_as_code` 所代表的行为。 参数包括：无显式参数。"""
        try:
            raise ValueError("boom")
        except ValueError as exc:
            info = json.loads(build_error_info(exc))
        assert info["code"] == "ValueError"
        assert info["context"] == {}

    def test_long_traceback_trimmed(self):
        # 用异常链构造长堆栈：递归帧会被 Python 折叠，链式 raise 每节都完整保留
        """验证 `long_traceback_trimmed` 所代表的行为。 参数包括：无显式参数。"""
        chained: BaseException = ValueError("root")
        for i in range(15):
            try:
                raise chained
            except BaseException as caught:  # noqa: BLE001
                wrapped = TaskExecutionError(f"wrap{i}")
                wrapped.__cause__ = caught  # 等价于 raise ... from ...
                chained = wrapped
        try:
            raise chained
        except TaskExecutionError as exc:
            info = json.loads(build_error_info(exc))
        tb_lines = info["traceback"].splitlines()
        assert tb_lines[0].startswith("...（共 ")
        assert len(tb_lines) <= 32  # 1 行裁剪说明 + 最多 30 行 + 末尾异常行


class TestRecorderProtocol:
    def test_protocol_implementable(self):
        """验证 `protocol_implementable` 所代表的行为。 参数包括：无显式参数。"""
        class DbRecorder:
            def record_error(self, task_id: int, error_info: str) -> None:
                """执行 `record_error` 对应的功能并返回处理结果。 参数包括：`task_id`、`error_info`。"""
                pass

        assert isinstance(DbRecorder(), TaskErrorRecorder)
