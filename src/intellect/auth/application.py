"""对外业务门面：授权与写入处于同一事务，内部服务沿用保存点。"""

import sqlite3
import time

from intellect.auth.passwords import hash_password
from intellect.auth.service import AuthService, User, UserManagementError, next_user_id
from intellect.core.errors import IntellectError
from intellect.doc.importing import DocumentImportService
from intellect.knowledge.aliases import AliasService
from intellect.knowledge.categories import CategoryService
from intellect.search.fts import FtsSearchService


class OperationUnavailableError(IntellectError):
    """已规划的业务操作尚未实现。"""

    code = "OPERATION_UNAVAILABLE"


class AuthorizedApplication:
    """所有外部业务调用经过 execute；连接必须由本应用独占事务。"""

    def __init__(self, connection: sqlite3.Connection):
        """装配登录服务，复用调用方连接但不关闭它。"""
        self.connection = connection
        self.auth = AuthService(connection)

    def execute(self, token: str, action: str, **payload):
        """先核验会话和权限再调用固定处理器，异常时回滚整个业务事务。"""
        if self.connection.in_transaction:
            raise UserManagementError("应用入口要求无在途事务的独立连接")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            actor = self.auth.require(token, action)
            if action == "user.create":
                return self._create_user(**payload)
            if action == "user.password":
                return self._set_password(**payload)
            if action == "search":
                return FtsSearchService(self.connection).search(**payload)
            if action == "document.import":
                payload["uploader"] = str(actor.id)
                return DocumentImportService(self.connection).register(**payload)
            if action == "knowledge.read":
                return self._read_knowledge(**payload)
            if action.startswith("category."):
                method = action.split(".", 1)[1]
                if method in {"rename", "move", "set_status", "delete"}:
                    payload["operator"] = str(actor.id)
                return getattr(CategoryService(self.connection), method)(**payload)
            if action.startswith("alias."):
                return getattr(AliasService(self.connection), action.split(".", 1)[1])(**payload)
            raise OperationUnavailableError("该业务尚未实现", context={"action": action})

    def _create_user(self, name: str, password: str, permissions: int = 1) -> User:
        """在授权写事务内创建用户；从持久化最大 ID 恢复雪花序列。"""
        if not isinstance(name, str) or not name.strip():
            raise UserManagementError("用户名不能为空")
        if type(permissions) is not int or permissions not in (0, 1):
            raise UserManagementError("permissions 必须为 0 或 1")
        try:
            encoded = hash_password(password)
        except ValueError as exc:
            raise UserManagementError(str(exc)) from exc
        previous = self.connection.execute('SELECT COALESCE(MAX(id), 1) FROM "user"').fetchone()[0]
        user_id = next_user_id(previous, time.time_ns() // 1_000_000)
        try:
            self.connection.execute(
                'INSERT INTO "user" (id, name, password, permissions) VALUES (?, ?, ?, ?)',
                (user_id, name, encoded, permissions),
            )
        except sqlite3.IntegrityError as exc:
            raise UserManagementError("用户名或用户 ID 已存在") from exc
        return User(user_id, name, permissions)

    def _set_password(self, user_id: int, password: str) -> None:
        """管理员重置指定用户密码；该用户旧会话在下一次调用时失效。"""
        try:
            encoded = hash_password(password)
        except ValueError as exc:
            raise UserManagementError(str(exc)) from exc
        cursor = self.connection.execute('UPDATE "user" SET password = ? WHERE id = ?', (encoded, user_id))
        if cursor.rowcount != 1:
            raise UserManagementError("用户不存在")

    def _read_knowledge(self, knowledge_id: int) -> dict:
        """只读取启用知识的当前版本，不暴露历史候选或停用内容。"""
        row = self.connection.execute(
            "SELECT k.id, v.id, v.title, v.content FROM knowledge k "
            "JOIN knowledge_version v ON v.id = k.current_version_id AND v.knowledge_id = k.id "
            "WHERE k.id = ? AND k.status = 0", (knowledge_id,),
        ).fetchone()
        if row is None:
            raise OperationUnavailableError("有效知识不存在")
        return dict(zip(("knowledge_id", "version_id", "title", "content"), row))
