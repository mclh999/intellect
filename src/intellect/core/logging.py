"""统一日志基线（T0.4）。

全系统唯一日志格式（控制台与文件一致），示例：

    2026-10-26 10:00:00 | INFO     | intellect.review.worker | 任务 42 执行失败: 嵌入服务超时

使用约定：
- 业务代码一律 ``logging.getLogger(__name__)`` 取 logger，模块路径自动成为
  ``intellect.<模块>.<文件>`` 前缀，禁止各模块自建格式或 print 记录业务事件；
- 进程入口调用一次 :func:`setup_logging`（重复调用幂等），日志级别与目录来自
  配置中心（``settings.logging``）；
- 日志文件在 ``settings.logging.dir`` 下按大小轮转，属运行产物，不入库。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from intellect.core.config import Settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_NAME = "intellect.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

# 第三方库日志降噪（只影响自身冗余输出，不影响 intellect 记录）
_NOISY_LOGGERS = ("httpx", "httpcore", "chromadb", "urllib3", "asyncio")

_configured = False


def setup_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    """按配置初始化根日志：控制台（stderr）+ 轮转文件，统一 LOG_FORMAT。

    ``force=True`` 用于测试或运行期调整级别时重建 handler。
    """
    global _configured
    if _configured and not force:
        return

    level = logging.INFO
    log_dir = Path("logs")
    if settings is not None:
        level = logging.getLevelName(settings.logging.level)
        log_dir = Path(settings.logging.dir)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / LOG_FILE_NAME,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True
