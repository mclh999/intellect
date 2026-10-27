"""已确认别名管理；待审建议由审核模块保存，不进入 alias。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from intellect.db.connection import ALIAS_COLLATION
from intellect.knowledge._common import (
    KnowledgeManagementError, atomic, require_enum, require_id, require_owner, require_text,
)
from intellect.knowledge.fts import refresh_owner


@dataclass(frozen=True)
class Alias:
    id: int
    owner_type: int
    owner_id: int
    name: str
    origin: int


class AliasService:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get(self, alias_id: int) -> Alias:
        require_id(alias_id)
        row = self.connection.execute(
            "SELECT id, owner_type, owner_id, name, origin FROM alias WHERE id = ?", (alias_id,)
        ).fetchone()
        if row is None:
            raise KnowledgeManagementError("别名不存在", context={"alias_id": alias_id})
        return Alias(*row)

    def list_for(self, owner_type: int, owner_id: int) -> list[Alias]:
        require_owner(self.connection, owner_type, owner_id)
        return [Alias(*row) for row in self.connection.execute(
            "SELECT id, owner_type, owner_id, name, origin FROM alias "
            "WHERE owner_type = ? AND owner_id = ? ORDER BY id", (owner_type, owner_id)
        )]

    def _find(self, owner_type: int, owner_id: int, name: str) -> Alias | None:
        row = self.connection.execute(
            "SELECT id, owner_type, owner_id, name, origin FROM alias "
            f"WHERE owner_type = ? AND owner_id = ? AND name = ? COLLATE {ALIAS_COLLATION}",
            (owner_type, owner_id, name),
        ).fetchone()
        return Alias(*row) if row is not None else None

    @staticmethod
    def _require_confirmed(confirmed: bool) -> None:
        if confirmed is not True:
            raise KnowledgeManagementError("待审别名须保存在 suggestion_payload，确认后才能写入")

    def add(self, owner_type: int, owner_id: int, name: str, *, confirmed: bool, origin: int = 1) -> Alias:
        self._require_confirmed(confirmed)
        require_enum(origin, (0, 1))
        require_text(name)
        with atomic(self.connection):
            require_owner(self.connection, owner_type, owner_id)
            existing = self._find(owner_type, owner_id, name)
            if existing:
                return existing
            alias_id = self.connection.execute(
                "INSERT INTO alias (owner_type, owner_id, name, origin) VALUES (?, ?, ?, ?)",
                (owner_type, owner_id, name, origin),
            ).lastrowid
            refresh_owner(self.connection, owner_type, owner_id)
            return self.get(alias_id)

    def rename(self, alias_id: int, name: str, *, confirmed: bool) -> Alias:
        self._require_confirmed(confirmed)
        require_text(name)
        with atomic(self.connection):
            before = self.get(alias_id)
            require_owner(self.connection, before.owner_type, before.owner_id)
            existing = self._find(before.owner_type, before.owner_id, name)
            if existing is not None and existing.id != alias_id:
                raise KnowledgeManagementError("该对象下已存在等价别名")
            if before.name == name:
                return before
            self.connection.execute("UPDATE alias SET name = ? WHERE id = ?", (name, alias_id))
            refresh_owner(self.connection, before.owner_type, before.owner_id)
            return self.get(alias_id)

    def delete(self, alias_id: int) -> None:
        with atomic(self.connection):
            before = self.get(alias_id)
            require_owner(self.connection, before.owner_type, before.owner_id)
            self.connection.execute("DELETE FROM alias WHERE id = ?", (alias_id,))
            refresh_owner(self.connection, before.owner_type, before.owner_id)
