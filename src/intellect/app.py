"""应用入口：T0 阶段为空骨架，仅验证依赖安装与入口可运行。

后续阶段在此装配配置中心、统一日志与各业务模块。
"""

from __future__ import annotations

import intellect


def main() -> int:
    """空入口：打印版本与就绪信息，返回退出码。"""
    print(f"intellect {intellect.__version__} —— Agent 知识库系统")
    print("骨架入口运行正常：依赖安装成功。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
