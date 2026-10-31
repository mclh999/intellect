"""进程内登录会话与默认拒绝的角色校验。"""

from dataclasses import dataclass, field
import secrets
import sqlite3
import time

from intellect.auth.passwords import hash_password, verify_password
from intellect.core.config import get_settings
from intellect.core.errors import IntellectError

USER_ACTIONS = frozenset({
    "search", "knowledge.read", "document.import", "qa.ask",
    "category.get", "category.list_all", "alias.get", "alias.list_for",
})
ADMIN_ACTIONS = frozenset({
    "user.create", "user.password", "review.resolve", "knowledge.write", "knowledge.rollback",
    "category.create", "category.create_tree", "category.rename", "category.move",
    "category.set_status", "category.delete", "category.attach_knowledge", "category.detach_knowledge",
    "alias.add", "alias.rename", "alias.delete",
})


class AuthenticationError(IntellectError):
    """凭据错误或会话无效。"""

    code = "AUTHENTICATION_FAILED"


class PermissionDeniedError(IntellectError):
    """当前角色无权执行指定操作。"""

    code = "PERMISSION_DENIED"


class UserManagementError(IntellectError):
    """用户输入、唯一约束或 ID 分配失败。"""

    code = "USER_MANAGEMENT_FAILED"


@dataclass(frozen=True)
class User:
    id: int
    name: str
    permissions: int


@dataclass(frozen=True)
class _Session:
    user_id: int
    password_hash: str = field(repr=False)
    expires_at: float


class AuthService:
    """会话仅在实例内有效；业务入口必须在事务中重新读取角色。"""

    def __init__(self, connection: sqlite3.Connection):
        """绑定事实库连接并从配置中心取得会话有效期。"""
        self.connection = connection
        self._sessions: dict[str, _Session] = {}
        self._ttl = get_settings().auth.session_ttl_seconds
        self._dummy_hash = hash_password(secrets.token_urlsafe(32))

    def login(self, name: str, password: str) -> str:
        """验证登录名和密码并返回随机令牌；失败不区分账号是否存在。"""
        if not isinstance(name, str):
            raise AuthenticationError("用户名或密码错误")
        row = self.connection.execute(
            'SELECT id, password FROM "user" WHERE name = ?', (name,),
        ).fetchone()
        valid = verify_password(password, row[1] if row else self._dummy_hash)
        if not valid or row is None:
            raise AuthenticationError("用户名或密码错误")
        now = time.monotonic()
        self._sessions = {key: value for key, value in self._sessions.items() if value.expires_at > now}
        token = secrets.token_urlsafe(32)
        self._sessions[token] = _Session(row[0], row[1], now + self._ttl)
        return token

    def logout(self, token: str) -> None:
        """移除当前实例的指定会话，重复退出无副作用。"""
        self._sessions.pop(token, None)

    def current_user(self, token: str) -> User:
        """校验会话时效及当前密码版本，读取实时角色且不返回密码。"""
        session = self._sessions.get(token) if isinstance(token, str) else None
        if session is None or session.expires_at <= time.monotonic():
            if isinstance(token, str):
                self._sessions.pop(token, None)
            raise AuthenticationError("请先登录或重新登录")
        row = self.connection.execute(
            'SELECT id, name, permissions, password FROM "user" WHERE id = ?', (session.user_id,),
        ).fetchone()
        if row is None or row[3] != session.password_hash:
            self._sessions.pop(token, None)
            raise AuthenticationError("请先登录或重新登录")
        return User(*row[:3])

    def require(self, token: str, action: str) -> User:
        """按明确操作名单授权；未知角色与操作均拒绝，不信任客户端角色。"""
        user = self.current_user(token)
        if user.permissions in (0, 1) and action in USER_ACTIONS:
            return user
        if user.permissions == 0 and action in ADMIN_ACTIONS:
            return user
        raise PermissionDeniedError("无权执行此操作", context={"action": action})


def next_user_id(last_id: int, now_ms: int) -> int:
    """按单库节点 0 的雪花协议分配 ID；调用方须持有写锁并传入持久化最大 ID。"""
    elapsed = now_ms - 1_767_225_600_000  # 固定协议 epoch：2026-01-01 UTC。
    if not 0 <= elapsed < (1 << 41):
        raise UserManagementError("雪花 ID 时间超出范围")
    previous_ms = last_id >> 22 if last_id > 1 else -1
    if elapsed < previous_ms:
        raise UserManagementError("系统时钟回拨，暂不能创建用户")
    sequence = (last_id & 4095) + 1 if elapsed == previous_ms else 0
    if sequence > 4095:
        raise UserManagementError("本毫秒雪花 ID 序列耗尽，请稍后重试")
    result = (elapsed << 22) | sequence
    if result <= 1:
        raise UserManagementError("雪花 ID 与保留测试 ID 冲突")
    return result
