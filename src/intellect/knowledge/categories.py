"""分类树与知识分类关联管理（T1.5，DESIGN V0.5 §3.8）。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from intellect.knowledge._common import (
    KnowledgeManagementError, atomic, log_category_change, require_enum,
    require_id, require_owner, require_reason, require_text,
)
from intellect.knowledge.fts import refresh_owner


@dataclass(frozen=True)
class Category:
    id: int
    parent_id: int | None
    name: str
    status: int


@dataclass(frozen=True)
class CategorySeed:
    name: str
    children: tuple[CategorySeed, ...] = ()


class CategoryService:
    """复用调用方连接；独立调用原子提交，嵌入已有事务时使用保存点。"""

    def __init__(self, connection: sqlite3.Connection):
        """实现 `__init__` 生命周期钩子，初始化或维护对象状态。 参数包括：`connection`。"""
        self.connection = connection

    def get(self, category_id: int) -> Category:
        """执行 `get` 对应的功能并返回处理结果。 参数包括：`category_id`。"""
        require_id(category_id)
        row = self.connection.execute(
            "SELECT id, parent_id, name, status FROM category WHERE id = ?", (category_id,)
        ).fetchone()
        if row is None:
            raise KnowledgeManagementError("分类不存在", context={"category_id": category_id})
        return Category(*row)

    def list_all(self) -> list[Category]:
        """包含停用结构节点，以保留启用后代的完整父节点关系。"""
        return [Category(*row) for row in self.connection.execute(
            "SELECT id, parent_id, name, status FROM category ORDER BY id"
        )]

    def _check_parent(self, parent_id: int | None, category_id: int | None = None) -> None:
        """执行 `check_parent` 所属的内部处理步骤。 参数包括：`parent_id`、`category_id`。"""
        visited = set()
        while parent_id is not None:
            if parent_id == category_id or parent_id in visited:
                raise KnowledgeManagementError("分类父节点不能指向自身或形成循环")
            visited.add(parent_id)
            parent_id = self.get(parent_id).parent_id

    def create(self, name: str, *, parent_id: int | None = None) -> Category:
        """执行 `create` 对应的功能并返回处理结果。 参数包括：`name`、`parent_id`。"""
        require_text(name)
        with atomic(self.connection):
            self._check_parent(parent_id)
            category_id = self.connection.execute(
                "INSERT INTO category (name, parent_id) VALUES (?, ?)", (name, parent_id)
            ).lastrowid
            return self.get(category_id)

    def create_tree(self, seeds: Iterable[CategorySeed], *, parent_id: int | None = None) -> list[Category]:
        """按输入层级创建初始分类树；任一节点失败则整棵树回滚。"""
        created = []
        with atomic(self.connection):
            self._check_parent(parent_id)
            stack = [(iter(seeds), parent_id)]
            while stack:
                children, parent = stack[-1]
                try:
                    seed = next(children)
                except StopIteration:
                    stack.pop()
                    continue
                if not isinstance(seed, CategorySeed):
                    raise KnowledgeManagementError("初始分类树节点必须为 CategorySeed")
                category = self.create(seed.name, parent_id=parent)
                created.append(category)
                stack.append((iter(seed.children), category.id))
        return created

    def rename(self, category_id: int, name: str, *, reason: str, operator: str | None = None) -> Category:
        """执行 `rename` 对应的功能并返回处理结果。 参数包括：`category_id`、`name`、`reason`、`operator`。"""
        require_text(name)
        require_reason(reason)
        with atomic(self.connection):
            before = self.get(category_id)
            if before.name == name:
                return before
            self.connection.execute("UPDATE category SET name = ? WHERE id = ?", (name, category_id))
            refresh_owner(self.connection, 1, category_id)
            after = self.get(category_id)
            log_category_change(self.connection, category_id, asdict(before), asdict(after), reason, operator)
            return after

    def move(self, category_id: int, parent_id: int | None, *, reason: str, operator: str | None = None) -> Category:
        """执行 `move` 对应的功能并返回处理结果。 参数包括：`category_id`、`parent_id`、`reason`、`operator`。"""
        require_reason(reason)
        with atomic(self.connection):
            before = self.get(category_id)
            self._check_parent(parent_id, category_id)
            if before.parent_id == parent_id:
                return before
            self.connection.execute("UPDATE category SET parent_id = ? WHERE id = ?", (parent_id, category_id))
            after = self.get(category_id)
            log_category_change(self.connection, category_id, asdict(before), asdict(after), reason, operator)
            return after

    def set_status(self, category_id: int, status: int, *, reason: str, operator: str | None = None) -> Category:
        """执行 `set_status` 对应的功能并返回处理结果。 参数包括：`category_id`、`status`、`reason`、`operator`。"""
        require_enum(status, (0, 1))
        require_reason(reason)
        with atomic(self.connection):
            before = self.get(category_id)
            if before.status == status:
                return before
            self.connection.execute("UPDATE category SET status = ? WHERE id = ?", (status, category_id))
            refresh_owner(self.connection, 1, category_id)
            after = self.get(category_id)
            log_category_change(self.connection, category_id, asdict(before), asdict(after), reason, operator)
            return after

    def delete(self, category_id: int, *, reason: str, operator: str | None = None) -> None:
        """执行 `delete` 对应的功能并返回处理结果。 参数包括：`category_id`、`reason`、`operator`。"""
        require_reason(reason)
        with atomic(self.connection):
            category = self.get(category_id)
            if self.connection.execute("SELECT 1 FROM category WHERE parent_id = ? LIMIT 1", (category_id,)).fetchone():
                raise KnowledgeManagementError("分类存在子节点，不能删除")
            if self.connection.execute("SELECT 1 FROM knowledge_category WHERE category_id = ? LIMIT 1", (category_id,)).fetchone():
                raise KnowledgeManagementError("分类存在知识关联，不能删除")
            before = asdict(category)
            before["aliases"] = [dict(zip(("id", "name", "origin"), row)) for row in self.connection.execute(
                "SELECT id, name, origin FROM alias WHERE owner_type = 1 AND owner_id = ? ORDER BY id",
                (category_id,),
            )]
            self.connection.execute("DELETE FROM alias WHERE owner_type = 1 AND owner_id = ?", (category_id,))
            self.connection.execute("DELETE FROM category WHERE id = ?", (category_id,))
            log_category_change(self.connection, category_id, before, None, reason, operator)

    def attach_knowledge(self, category_id: int, knowledge_id: int) -> bool:
        """执行 `attach_knowledge` 对应的功能并返回处理结果。 参数包括：`category_id`、`knowledge_id`。"""
        with atomic(self.connection):
            self.get(category_id)
            require_owner(self.connection, 0, knowledge_id)
            if self.connection.execute(
                "SELECT 1 FROM knowledge_category WHERE category_id = ? AND knowledge_id = ?",
                (category_id, knowledge_id),
            ).fetchone():
                return False
            self.connection.execute(
                "INSERT INTO knowledge_category (category_id, knowledge_id) VALUES (?, ?)", (category_id, knowledge_id)
            )
            refresh_owner(self.connection, 0, knowledge_id)
            return True

    def detach_knowledge(self, category_id: int, knowledge_id: int) -> bool:
        """执行 `detach_knowledge` 对应的功能并返回处理结果。 参数包括：`category_id`、`knowledge_id`。"""
        with atomic(self.connection):
            self.get(category_id)
            require_owner(self.connection, 0, knowledge_id)
            deleted = self.connection.execute(
                "DELETE FROM knowledge_category WHERE category_id = ? AND knowledge_id = ?", (category_id, knowledge_id)
            ).rowcount
            if deleted:
                refresh_owner(self.connection, 0, knowledge_id)
            return bool(deleted)
