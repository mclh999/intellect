"""T0.4 统一日志基线测试：幂等初始化与统一格式。"""

from __future__ import annotations

import logging

import pytest

from intellect.core.config import Settings
from intellect.core.logging import LOG_FORMAT, setup_logging


@pytest.fixture(autouse=True)
def _reset_after_test():
    yield
    setup_logging(Settings(), force=True)


def test_setup_idempotent(tmp_path):
    settings = Settings()
    settings.logging.dir = str(tmp_path / "logs")
    setup_logging(settings, force=True)
    count = len(logging.getLogger().handlers)
    setup_logging(settings)  # 二次调用应直接返回
    assert len(logging.getLogger().handlers) == count == 2  # 控制台 + 文件


def test_format_matches_baseline():
    record = logging.LogRecord(
        name="intellect.review.worker",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="任务 %d 执行失败: %s",
        args=(42, "嵌入服务超时"),
        exc_info=None,
    )
    formatted = logging.Formatter(LOG_FORMAT).format(record)
    assert "| INFO     | intellect.review.worker | 任务 42 执行失败: 嵌入服务超时" in formatted
