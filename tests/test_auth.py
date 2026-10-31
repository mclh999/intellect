"""T1.6：登录、授权、事务、初始化与雪花 ID 的隔离库验收。"""

from contextlib import closing
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from intellect.auth import application as application_module
from intellect.auth import cli, service
from intellect.auth.application import AuthorizedApplication, OperationUnavailableError
from intellect.auth.passwords import hash_password, verify_password
from intellect.auth.service import AuthenticationError, PermissionDeniedError, UserManagementError, next_user_id
from intellect.core.config import AuthConfig, load_settings
from intellect.db import connect_database, initialize_database


@pytest.fixture
def app(tmp_path):
    """在独立临时库创建应用，测试结束关闭连接。"""
    path = tmp_path / "auth.db"
    initialize_database(path)
    with closing(connect_database(path)) as connection:
        yield AuthorizedApplication(connection)


@pytest.fixture
def admin(app):
    """用初始化测试账号登录并返回仅当前实例有效的令牌。"""
    return app.auth.login("admin", "12345")


@pytest.fixture
def regular(app, admin):
    """经管理员创建普通用户并登录，不直接伪造用户身份。"""
    app.execute(admin, "user.create", name="reader", password="reader-password")
    return app.auth.login("reader", "reader-password")


class TestAuthentication:
    def test_seed_and_password_storage(self, app, admin):
        """初始化管理员保留固定测试 ID，数据库与返回对象不暴露原始密码。"""
        row = app.connection.execute('SELECT id, name, password, permissions FROM "user"').fetchone()
        assert row[0:2] == (1, "admin") and row[3] == 0
        assert row[2] != "12345" and verify_password("12345", row[2])
        assert app.auth.current_user(admin).id == 1
        assert "password" not in repr(app.auth.current_user(admin))

    @pytest.mark.parametrize("name,password", [("admin", "wrong"), ("missing", "12345"), ("' OR 1=1--", "12345")])
    def test_bad_credentials(self, app, name, password):
        """错误密码、未知账号和 SQL 注入字符串统一认证失败。"""
        with pytest.raises(AuthenticationError, match="用户名或密码错误"):
            app.auth.login(name, password)

    def test_logout_expiry_and_instance_isolation(self, app, admin, monkeypatch):
        """退出、到期和另一服务实例都不能复用原会话。"""
        other = AuthorizedApplication(app.connection)
        with pytest.raises(AuthenticationError):
            other.auth.current_user(admin)
        app.auth.logout(admin)
        with pytest.raises(AuthenticationError):
            app.auth.current_user(admin)
        token = app.auth.login("admin", "12345")
        expiry = app.auth._sessions[token].expires_at
        monkeypatch.setattr(service.time, "monotonic", lambda: expiry)
        with pytest.raises(AuthenticationError):
            app.auth.current_user(token)

    def test_password_change_revokes_all_sessions(self, app, admin, regular):
        """重置密码使旧会话失效，原密码拒绝，新密码可登录。"""
        user_id = app.auth.current_user(regular).id
        app.execute(admin, "user.password", user_id=user_id, password="changed")
        with pytest.raises(AuthenticationError):
            app.execute(regular, "category.list_all")
        with pytest.raises(AuthenticationError):
            app.auth.login("reader", "reader-password")
        assert app.auth.current_user(app.auth.login("reader", "changed")).id == user_id

    def test_salted_hash_and_malformed_hash(self):
        """相同密码使用独立随机盐，未知或损坏的存储格式均拒绝。"""
        first, second = hash_password("test"), hash_password("test")
        assert first != second and verify_password("test", first)
        for invalid in ("test", "12345", "pbkdf2_sha256$999999999999$00$00", None):
            assert not verify_password("test", invalid)


class TestAuthorization:
    def test_every_role_action_and_default_deny(self, app, admin, regular):
        """完整矩阵允许普通读取导入，拒绝普通用户全部管理操作，未知操作默认拒绝。"""
        for action in service.USER_ACTIONS:
            assert app.auth.require(regular, action).permissions == 1
            assert app.auth.require(admin, action).permissions == 0
        for action in service.ADMIN_ACTIONS:
            assert app.auth.require(admin, action).permissions == 0
            with pytest.raises(PermissionDeniedError):
                app.execute(regular, action)
        for token in (admin, regular):
            with pytest.raises(PermissionDeniedError):
                app.execute(token, "category.__class__")
        with pytest.raises(AuthenticationError):
            app.execute("forged", "category.create", name="不应写入")
        assert app.connection.execute("SELECT count(*) FROM category").fetchone()[0] == 0

    def test_role_is_reloaded(self, app, admin):
        """已有管理员会话在角色降级后不能继续修改分类。"""
        with app.connection:
            app.connection.execute('UPDATE "user" SET permissions = 1 WHERE id = 1')
        with pytest.raises(PermissionDeniedError):
            app.execute(admin, "category.create", name="禁止")

    def test_regular_import_search_and_read(self, app, regular, tmp_path):
        """普通用户可以登记实际文本、检索及读分类，上传者由会话覆盖。"""
        archive = tmp_path / "input.txt"
        archive.write_text("事务保证数据一致性。", encoding="utf-8")
        result = app.execute(regular, "document.import", archive=archive, title="测试", kind="TXT", uploader="system")
        uploader = app.connection.execute("SELECT uploader_id FROM source_document WHERE id = ?", (result.document_id,)).fetchone()[0]
        assert uploader == str(app.auth.current_user(regular).id)
        assert app.execute(regular, "search", question="事务") == []
        assert app.execute(regular, "category.list_all") == []
        with pytest.raises(OperationUnavailableError):
            app.execute(regular, "qa.ask", question="事务是什么")

    def test_admin_write_and_actor(self, app, admin):
        """管理员能修改分类，审计操作者不能由请求伪造。"""
        category = app.execute(admin, "category.create", name="原分类")
        updated = app.execute(admin, "category.rename", category_id=category.id, name="新分类", reason="测试", operator="system")
        assert updated.name == "新分类"
        assert app.connection.execute("SELECT operator_id FROM change_log").fetchone()[0] == "1"

    def test_partial_business_failure_rolls_back(self, app, admin, monkeypatch):
        """业务处理器写入后异常时，应用事务不留下部分变更。"""
        def broken_create(instance, name):
            """模拟分类落库后失败，验证门面拥有的事务回滚。"""
            instance.connection.execute("INSERT INTO category (name) VALUES (?)", (name,))
            raise RuntimeError("中途失败")

        monkeypatch.setattr(application_module.CategoryService, "create", broken_create)
        with pytest.raises(RuntimeError):
            app.execute(admin, "category.create", name="半写")
        assert app.connection.execute("SELECT count(*) FROM category").fetchone()[0] == 0
        assert not app.connection.in_transaction

    def test_authorization_and_write_share_lock(self, app, admin, monkeypatch):
        """开始受保护写入后其他连接无法在检查与提交之间修改角色。"""
        path = app.connection.execute("PRAGMA database_list").fetchone()[2]
        original = application_module.CategoryService.create

        def checked_create(instance, name):
            """在实际写入前尝试另一连接降权，必须被写锁阻止。"""
            with closing(connect_database(path)) as other:
                other.execute("PRAGMA busy_timeout=0")
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute('UPDATE "user" SET permissions=1 WHERE id=1')
            return original(instance, name)

        monkeypatch.setattr(application_module.CategoryService, "create", checked_create)
        assert app.execute(admin, "category.create", name="原子授权").name == "原子授权"


class TestUsersAndIds:
    def test_user_creation_duplicate_and_restart(self, app, admin, monkeypatch):
        """同毫秒和新服务实例延续持久化序列，重复名称失败且不增加用户。"""
        monkeypatch.setattr(application_module.time, "time_ns", lambda: 1_800_000_000_000_000_000)
        first = app.execute(admin, "user.create", name="first", password="pw")
        other = AuthorizedApplication(app.connection)
        token = other.auth.login("admin", "12345")
        second = other.execute(token, "user.create", name="second", password="pw")
        assert first.id > 1 and second.id == first.id + 1
        assert ((first.id >> 12) & 1023) == 0
        with pytest.raises(UserManagementError):
            app.execute(admin, "user.create", name="first", password="pw")
        assert app.connection.execute('SELECT count(*) FROM "user"').fetchone()[0] == 3

    def test_concurrent_connections_allocate_unique_ids(self, app, monkeypatch):
        """多连接并发创建在同毫秒串行领取不同序列，不依赖进程内计数。"""
        path = app.connection.execute("PRAGMA database_list").fetchone()[2]
        monkeypatch.setattr(application_module.time, "time_ns", lambda: 1_800_000_000_000_000_000)

        def create(index):
            """在线程自己的连接中登录并创建一个普通用户。"""
            with closing(connect_database(path)) as connection:
                application = AuthorizedApplication(connection)
                token = application.auth.login("admin", "12345")
                return application.execute(token, "user.create", name=f"u{index}", password="pw").id

        with ThreadPoolExecutor(max_workers=3) as pool:
            ids = list(pool.map(create, range(3)))
        assert len(set(ids)) == 3
        assert max(ids) - min(ids) == 2

    def test_snowflake_limits(self):
        """时钟回拨、序列耗尽和时间溢出时失败，不返回碰撞或超范围 ID。"""
        epoch = 1_767_225_600_000
        first = next_user_id(1, epoch + 100)
        assert first == 100 << 22
        assert next_user_id(first, epoch + 100) == first + 1
        assert next_user_id(first + 4095, epoch + 101) == 101 << 22
        for previous, now in ((first, epoch + 99), (first + 4095, epoch + 100), (1, epoch - 1), (1, epoch + (1 << 41))):
            with pytest.raises(UserManagementError):
                next_user_id(previous, now)

    @pytest.mark.parametrize("name,password,role", [("", "pw", 1), (" ", "pw", 1), ("a", "", 1), ("a", "pw", 2), ("a", "pw", True)])
    def test_invalid_users(self, app, admin, name, password, role):
        """非法用户名、密码或角色不会留下用户记录。"""
        with pytest.raises(UserManagementError):
            app.execute(admin, "user.create", name=name, password=password, permissions=role)
        assert app.connection.execute('SELECT count(*) FROM "user"').fetchone()[0] == 1

    def test_schema_rejects_implicit_id_and_invalid_role(self, app):
        """数据库拒绝省略 ID 的隐式分配及未定义角色。"""
        for sql in ('INSERT INTO "user" (name,password,permissions) VALUES (\'x\',\'x\',1)',
                    'INSERT INTO "user" VALUES (2,\'x\',\'x\',2)'):
            with pytest.raises(sqlite3.IntegrityError), app.connection:
                app.connection.execute(sql)


class TestConfigAndCli:
    @pytest.mark.parametrize("value", [0, -1, True, 1.5, "3600"])
    def test_session_ttl_validation(self, value):
        """会话有效期只接受正整数秒。"""
        with pytest.raises(ValidationError):
            AuthConfig(session_ttl_seconds=value)

    def test_toml_session_ttl(self, tmp_path):
        """配置文件的会话有效期覆盖默认值。"""
        path = tmp_path / "auth.toml"
        path.write_text("[auth]\nsession_ttl_seconds = 120\n", encoding="utf-8")
        assert load_settings(path).auth.session_ttl_seconds == 120

    def test_cli_login_and_denial(self, app, admin, monkeypatch, capsys):
        """命令行登录不输出密码或令牌，普通用户写操作返回非零。"""
        path = app.connection.execute("PRAGMA database_list").fetchone()[2]
        app.execute(admin, "user.create", name="reader", password="reader-password")
        monkeypatch.setattr(cli, "connect_database", lambda: connect_database(path))
        monkeypatch.setattr(cli, "getpass", lambda prompt: "reader-password")
        assert cli.run(["login", "--name", "reader"]) == 0
        output = capsys.readouterr().out
        assert "reader" in output and "password" not in output
        assert cli.run(["category-create", "--name", "reader", "禁止"]) == 1
        assert "PERMISSION_DENIED" in capsys.readouterr().err
