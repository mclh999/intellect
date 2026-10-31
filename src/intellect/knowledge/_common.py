"""管理服务共用的事务和目标校验；不提交调用方已经开启的事务。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from intellect.core.errors import IntellectError


class KnowledgeManagementError(IntellectError):
    code = "KNOWLEDGE_MANAGEMENT_FAILED"


@contextmanager
def atomic(connection: sqlite3.Connection):
    """执行 `atomic` 对应的功能并返回处理结果。 参数包括：`connection`。"""
    nested = connection.in_transaction
    savepoint = "management_" + uuid4().hex
    if nested:
        connection.execute(f"SAVEPOINT {savepoint}")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        if nested:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            connection.commit()
    except BaseException:
        if nested:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            connection.rollback()
        raise


def require_id(value: int) -> None:
    """执行 `require_id` 对应的功能并返回处理结果。 参数包括：`value`。"""
    if type(value) is not int or value <= 0:
        raise KnowledgeManagementError("对象 ID 必须为正整数")


def require_enum(value: int, allowed: tuple[int, ...]) -> None:
    """执行 `require_enum` 对应的功能并返回处理结果。 参数包括：`value`、`allowed`。"""
    if type(value) is not int or value not in allowed:
        raise KnowledgeManagementError("枚举值无效", context={"value": value})


def require_text(value: str) -> None:
    """执行 `require_text` 对应的功能并返回处理结果。 参数包括：`value`。"""
    if not isinstance(value, str):
        raise KnowledgeManagementError("名称必须为文本")


def require_reason(reason: str) -> None:
    """执行 `require_reason` 对应的功能并返回处理结果。 参数包括：`reason`。"""
    if not isinstance(reason, str) or not reason.strip():
        raise KnowledgeManagementError("分类变更必须填写原因")


def require_owner(connection: sqlite3.Connection, owner_type: int, owner_id: int) -> None:
    """执行 `require_owner` 对应的功能并返回处理结果。 参数包括：`connection`、`owner_type`、`owner_id`。"""
    require_enum(owner_type, (0, 1))
    require_id(owner_id)
    table = "knowledge" if owner_type == 0 else "category"
    if connection.execute(f"SELECT 1 FROM {table} WHERE id = ?", (owner_id,)).fetchone() is None:
        raise KnowledgeManagementError("别名或分类关联的目标不存在", context={
            "owner_type": owner_type, "owner_id": owner_id,
        })


def log_category_change(connection, category_id, before, after, reason, operator):
    """执行 `log_category_change` 对应的功能并返回处理结果。 参数包括：`connection`、`category_id`、`before`、`after`、`reason`、`operator`。"""
    connection.execute(
        "INSERT INTO change_log (target_type, target_id, old_value, new_value, "
        "change_reason, operator_id) VALUES (0, ?, ?, ?, ?, ?)",
        (category_id, json.dumps(before, ensure_ascii=False),
         json.dumps(after, ensure_ascii=False) if after is not None else None, reason, operator),
    )
