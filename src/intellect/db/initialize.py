"""新库初始化：V0.11 DDL 原子执行，不承担既有事实库迁移（T1.1）。"""

from __future__ import annotations

import logging
import sqlite3
from importlib.resources import files
from pathlib import Path

from intellect.core.errors import IntellectError
from intellect.db.connection import _database_path, _open_connection

logger = logging.getLogger(__name__)


class DatabaseInitializationError(IntellectError):
    """新库初始化失败或目标库已有结构。"""

    code = "DATABASE_INITIALIZATION_FAILED"


def _require_empty_database(connection: sqlite3.Connection) -> None:
    """执行 `require_empty_database` 所属的内部处理步骤。 参数包括：`connection`。"""
    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 1"
    ).fetchone()
    if existing:
        raise DatabaseInitializationError("目标数据库已有结构，请先评审迁移，禁止重跑建表脚本")


def _execute_schema(connection: sqlite3.Connection, schema: str) -> None:
    # executescript 会先提交在途事务；逐条执行 DDL，保证中途失败全部回滚。
    """执行 `execute_schema` 所属的内部处理步骤。 参数包括：`connection`、`schema`。"""
    statement = ""
    for line in schema.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        connection.execute(statement)  # 文件尾部注释或不完整 SQL（后者须报错）


def initialize_database(path: str | Path | None = None) -> None:
    """在空库创建 17 张业务表及 FTS5，已有结构时报错并保留原数据。

    WAL 在 DDL 事务之前设置；表、索引与测试管理员在一个 BEGIN IMMEDIATE 事务中建立。
    失败回滚结构和初始化数据，不删除数据库文件；空库修正问题后可重试初始化。
    """
    target = _database_path(path)
    schema = files("intellect.db").joinpath("schema.sql").read_text(encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = _open_connection(target, create=True)
    try:
        _require_empty_database(connection)
        if connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
            raise DatabaseInitializationError("无法启用 SQLite WAL 模式")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_empty_database(connection)
            _execute_schema(connection, schema)
    except sqlite3.Error as exc:
        raise DatabaseInitializationError(
            "数据库建表失败", context={"path": str(target)}
        ) from exc
    finally:
        connection.close()
    logger.info("数据库 V0.11 初始化完成：%s", target)
