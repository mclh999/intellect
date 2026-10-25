"""应用入口：装配配置中心与统一日志（T0 阶段为空骨架入口）。

后续阶段在此接入各业务模块与后台任务。
"""

from __future__ import annotations

import logging

import intellect
from intellect.core.config import get_settings
from intellect.core.logging import setup_logging

logger = logging.getLogger(__name__)


def main() -> int:
    """空入口：加载配置、初始化统一日志，打印就绪信息，返回退出码。"""
    settings = get_settings()
    setup_logging(settings)

    print(f"intellect {intellect.__version__} —— Agent 知识库系统")
    print("配置中心就绪（默认值与设计稿一致）：")
    print(
        f"  去重阈值：自动合并 s>={settings.dedup.auto_merge_threshold:.2f}，"
        f"灰区 s>={settings.dedup.gray_zone_threshold:.2f}"
    )
    print(f"  FTS 短问题词数阈值：{settings.fts.short_question_max_terms}")
    print(f"  切分参数：size={settings.chunk.chunk_size} overlap={settings.chunk.chunk_overlap}")

    logger.info("骨架入口启动：配置中心与统一日志基线就绪")
    logger.warning("示例日志行（统一格式，控制台与 logs/intellect.log 一致）")
    print("骨架入口运行正常：依赖、配置与日志均可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
