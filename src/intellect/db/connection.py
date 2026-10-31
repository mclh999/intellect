"""SQLite 连接工厂：固定 PRAGMA 与 V0.4 别名比较规则（T1.2）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from intellect.core.config import get_settings

ALIAS_COLLATION = "INTELLECT_ALIAS_V1"


def alias_comparison_key(name: str) -> str:
    """大小写折叠；原串仅有一个普通空格时忽略它，其他空白保留。"""
    key = name.casefold()
    return key.replace(" ", "") if name.count(" ") == 1 else key


def _compare_aliases(left: str, right: str) -> int:
    """执行 `compare_aliases` 所属的内部处理步骤。 参数包括：`left`、`right`。"""
    left_key = alias_comparison_key(left)
    right_key = alias_comparison_key(right)
    return (left_key > right_key) - (left_key < right_key)


def _database_path(path: str | Path | None) -> Path:
    """执行 `database_path` 所属的内部处理步骤。 参数包括：`path`。"""
    return Path(get_settings().database.path if path is None else path).resolve()


def _open_connection(path: Path, *, create: bool) -> sqlite3.Connection:
    # URI mode=rw 防止普通连接因路径拼写错误创建空事实库。
    """执行 `open_connection` 所属的内部处理步骤。 参数包括：`path`、`create`。"""
    mode = "rwc" if create else "rw"
    connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True)
    try:
        connection.create_collation(ALIAS_COLLATION, _compare_aliases)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = NORMAL")
    except BaseException:
        connection.close()
        raise
    return connection


def connect_database(path: str | Path | None = None) -> sqlite3.Connection:
    """连接已有库，缺省路径读取配置中心；调用方负责事务和关闭连接。

    不创建缺失的库，不执行 WAL 设置或建表；首次建库用 initialize_database。
    sqlite3 的连接上下文管理器只提交/回滚事务，不关闭连接。
    """
    return _open_connection(_database_path(path), create=False)
