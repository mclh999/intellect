"""T1.1–T1.3：隔离文件库验证 PRAGMA、V0.4 约束与原子性。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from intellect.core.config import Settings
from intellect.db import (
    ALIAS_COLLATION,
    DatabaseInitializationError,
    alias_comparison_key,
    connect_database,
    initialize_database,
)
from intellect.db import connection as connection_module
from intellect.db import initialize as initialize_module

BUSINESS_TABLES = {
    "embedding_model", "source_document", "document_chunk", "knowledge",
    "knowledge_version", "knowledge_source", "category", "knowledge_category",
    "alias", "task_process", "candidate", "review_issue", "change_log",
    "qa_record", "qa_citation", "extraction_result", "user",
}


@pytest.fixture
def database_path(tmp_path):
    """执行 `database_path` 对应的功能并返回处理结果。 参数包括：`tmp_path`。"""
    path = tmp_path / "知识 #1" / "test.db"
    initialize_database(path)
    return path


@pytest.fixture
def connection(database_path):
    """执行 `connection` 对应的功能并返回处理结果。 参数包括：`database_path`。"""
    with closing(connect_database(database_path)) as database:
        yield database


def _knowledge(database):
    """执行 `knowledge` 所属的内部处理步骤。 参数包括：`database`。"""
    knowledge_id = database.execute("INSERT INTO knowledge DEFAULT VALUES").lastrowid
    version_id = database.execute(
        "INSERT INTO knowledge_version (knowledge_id, version_no, content, content_hash, "
        "change_type) VALUES (?, 1, '完整正文', 'hash', 0)", (knowledge_id,),
    ).lastrowid
    database.execute(
        "UPDATE knowledge SET current_version_id = ? WHERE id = ?", (version_id, knowledge_id)
    )
    return knowledge_id, version_id


class TestConnection:
    def test_every_connection_configures_pragmas_and_collation(self, database_path):
        """验证 `every_connection_configures_pragmas_and_collation` 所代表的行为。 参数包括：`database_path`。"""
        for _ in range(2):
            with closing(connect_database(database_path)) as database:
                assert database.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                assert database.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
                assert database.execute("PRAGMA synchronous").fetchone()[0] == 1
                assert database.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
                assert database.execute(
                    f"SELECT ? = ? COLLATE {ALIAS_COLLATION}", ("AI", "a i")
                ).fetchone()[0] == 1

    def test_connect_does_not_set_wal_again(self, database_path, monkeypatch):
        """验证 `connect_does_not_set_wal_again` 所代表的行为。 参数包括：`database_path`、`monkeypatch`。"""
        statements = []
        real_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            """执行 `traced_connect` 对应的功能并返回处理结果。 参数包括：`*args`、`**kwargs`。"""
            database = real_connect(*args, **kwargs)
            database.set_trace_callback(statements.append)
            return database

        monkeypatch.setattr(connection_module.sqlite3, "connect", traced_connect)
        with closing(connect_database(database_path)):
            pass
        assert not any("journal_mode" in statement.lower() for statement in statements)

    def test_default_path_from_settings(self, database_path, monkeypatch):
        """验证 `default_path_from_settings` 所代表的行为。 参数包括：`database_path`、`monkeypatch`。"""
        monkeypatch.setattr(connection_module, "get_settings", lambda: Settings(
            database={"path": str(database_path)}
        ))
        with closing(connect_database()) as database:
            assert database.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0

    def test_missing_database_is_not_created(self, tmp_path):
        """验证 `missing_database_is_not_created` 所代表的行为。 参数包括：`tmp_path`。"""
        path = tmp_path / "missing.db"
        with pytest.raises(sqlite3.OperationalError):
            connect_database(path)
        assert not path.exists()


class TestInitialization:
    def test_schema_has_exact_business_tables_and_fts(self, connection):
        """验证 `schema_has_exact_business_tables_and_fts` 所代表的行为。 参数包括：`connection`。"""
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )}
        assert tables == BUSINESS_TABLES | {
            "knowledge_fts", "knowledge_fts_data", "knowledge_fts_idx",
            "knowledge_fts_content", "knowledge_fts_docsize", "knowledge_fts_config",
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'knowledge_fts'"
        ).fetchone()[0]
        assert "knowledge_id UNINDEXED" in sql
        assert "version_id   UNINDEXED" in sql
        assert "unicode61" in sql
        connection.execute(
            "INSERT INTO knowledge_fts (rowid, content_tokens, knowledge_id, version_id) "
            "VALUES (1, '事务 隔离', 1, 2)"
        )
        assert connection.execute(
            "SELECT rowid, knowledge_id, version_id FROM knowledge_fts "
            "WHERE knowledge_fts MATCH ?", ('"事务" OR "不存在"',)
        ).fetchall() == [(1, 1, 2)]

    def test_existing_database_is_preserved(self, database_path):
        """验证 `existing_database_is_preserved` 所代表的行为。 参数包括：`database_path`。"""
        with closing(connect_database(database_path)) as database, database:
            database.execute("INSERT INTO category (name) VALUES ('已有分类')")
        with pytest.raises(DatabaseInitializationError, match="已有结构"):
            initialize_database(database_path)
        with closing(connect_database(database_path)) as database:
            assert database.execute("SELECT name FROM category").fetchall() == [("已有分类",)]

    def test_unrecognized_existing_schema_is_not_changed(self, tmp_path):
        """验证 `unrecognized_existing_schema_is_not_changed` 所代表的行为。 参数包括：`tmp_path`。"""
        path = tmp_path / "existing.db"
        with closing(sqlite3.connect(path)) as database, database:
            database.execute("CREATE TABLE original (value TEXT)")
            database.execute("INSERT INTO original VALUES ('保留')")
            mode = database.execute("PRAGMA journal_mode").fetchone()[0]
        with pytest.raises(DatabaseInitializationError):
            initialize_database(path)
        with closing(sqlite3.connect(path)) as database:
            assert database.execute("SELECT * FROM original").fetchall() == [("保留",)]
            assert database.execute("PRAGMA journal_mode").fetchone()[0] == mode

    def test_schema_failure_rolls_back_all_ddl_and_allows_retry(self, tmp_path, monkeypatch):
        """验证 `schema_failure_rolls_back_all_ddl_and_allows_retry` 所代表的行为。 参数包括：`tmp_path`、`monkeypatch`。"""
        path = tmp_path / "failed.db"
        execute_schema = initialize_module._execute_schema

        def broken_schema(database, schema):
            """执行 `broken_schema` 对应的功能并返回处理结果。 参数包括：`database`、`schema`。"""
            execute_schema(database, schema + "\nINSERT INTO missing_table VALUES (1);")

        monkeypatch.setattr(initialize_module, "_execute_schema", broken_schema)
        with pytest.raises(DatabaseInitializationError) as caught:
            initialize_database(path)
        assert caught.value.code == "DATABASE_INITIALIZATION_FAILED"
        assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        with closing(sqlite3.connect(path)) as database:
            assert database.execute("SELECT name FROM sqlite_master").fetchall() == []
        monkeypatch.setattr(initialize_module, "_execute_schema", execute_schema)
        initialize_database(path)
        with closing(connect_database(path)) as database:
            assert database.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0


class TestAliasConstraints:
    @pytest.mark.parametrize("left,right,equal", [
        ("AI", "ai", True), ("AI", "A I", True), ("AI", " AI", True),
        ("AI", "AI ", True), ("AI", "A  I", False), ("AI", " A I", False),
        ("AI", "A I ", False), ("AI", "A\tI", False), ("AI", "A\nI", False),
        ("AI", "A\u3000I", False), ("AI", "ＡＩ", False),
        ("A  I", "a  i", True), ("Straße", "STRASSE", True),
    ])
    def test_python_key_and_unique_index_agree(self, connection, left, right, equal):
        """验证 `python_key_and_unique_index_agree` 所代表的行为。 参数包括：`connection`、`left`、`right`、`equal`。"""
        assert (alias_comparison_key(left) == alias_comparison_key(right)) is equal
        connection.execute(
            "INSERT INTO alias (owner_type, owner_id, name) VALUES (0, 1, ?)", (left,)
        )
        if equal:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO alias (owner_type, owner_id, name, origin) VALUES (0, 1, ?, 1)",
                    (right,),
                )
        else:
            connection.execute(
                "INSERT INTO alias (owner_type, owner_id, name) VALUES (0, 1, ?)", (right,)
            )
        assert connection.execute("SELECT name FROM alias ORDER BY id").fetchall() == (
            [(left,)] if equal else [(left,), (right,)]
        )

    def test_same_name_allowed_for_other_owners_and_owner_types(self, connection):
        """验证 `same_name_allowed_for_other_owners_and_owner_types` 所代表的行为。 参数包括：`connection`。"""
        connection.executemany(
            "INSERT INTO alias (owner_type, owner_id, name) VALUES (?, ?, 'AI')",
            [(0, 1), (0, 2), (1, 1)],
        )
        assert connection.execute("SELECT count(*) FROM alias").fetchone()[0] == 3

    def test_update_conflict_preserves_original_name(self, connection):
        """验证 `update_conflict_preserves_original_name` 所代表的行为。 参数包括：`connection`。"""
        connection.executemany(
            "INSERT INTO alias (owner_type, owner_id, name) VALUES (0, 1, ?)", [("AI",), ("ML",)]
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE alias SET name = 'a i' WHERE name = 'ML'")
        assert connection.execute("SELECT name FROM alias WHERE name = 'ML'").fetchone() == ("ML",)

    def test_missing_collation_fails_instead_of_bypassing_unique_index(self, database_path):
        """验证 `missing_collation_fails_instead_of_bypassing_unique_index` 所代表的行为。 参数包括：`database_path`。"""
        with closing(sqlite3.connect(database_path)) as database:
            with pytest.raises(sqlite3.OperationalError, match=ALIAS_COLLATION):
                database.execute("INSERT INTO alias (owner_type, owner_id, name) VALUES (0, 1, 'AI')")

    def test_collated_lookup_finds_equivalent_name(self, connection):
        """验证 `collated_lookup_finds_equivalent_name` 所代表的行为。 参数包括：`connection`。"""
        connection.execute("INSERT INTO alias (owner_type, owner_id, name) VALUES (0, 1, 'AI')")
        assert connection.execute(
            f"SELECT name FROM alias WHERE owner_type = 0 AND owner_id = 1 "
            f"AND name = ? COLLATE {ALIAS_COLLATION}", ("a i",)
        ).fetchall() == [("AI",)]


class TestRelationalConstraints:
    def test_embedding_active_partial_unique_index(self, connection):
        """验证 `embedding_active_partial_unique_index` 所代表的行为。 参数包括：`connection`。"""
        connection.executemany(
            "INSERT INTO embedding_model (model_name, dimension, is_active) VALUES (?, 1024, ?)",
            [("active", 1), ("inactive-1", 0), ("inactive-2", 0)],
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE embedding_model SET is_active = 1 WHERE model_name = 'inactive-1'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO embedding_model (model_name, dimension, is_active) VALUES ('active-2', 1024, 1)")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO embedding_model (model_name, dimension) VALUES ('active', 1024)")

    @pytest.mark.parametrize("sql", [
        "INSERT INTO document_chunk (source_document_id, chunk_index, content, chunk_hash) VALUES (999, 0, '原文', 'h')",
        "INSERT INTO knowledge_version (knowledge_id, version_no, content, content_hash, change_type) VALUES (999, 1, '正文', 'h', 0)",
        "INSERT INTO knowledge_source (knowledge_id, chunk_id) VALUES (999, 999)",
        "INSERT INTO category (parent_id, name) VALUES (999, '子分类')",
        "INSERT INTO knowledge_category (knowledge_id, category_id) VALUES (999, 999)",
        "INSERT INTO task_process (task_type, source_document_id) VALUES (0, 999)",
        "INSERT INTO candidate (content, task_process_id, chunk_id) VALUES ('候选', 999, 999)",
        "INSERT INTO review_issue (candidate_id, issue_type) VALUES (999, 0)",
        "INSERT INTO qa_citation (qa_record_id, knowledge_id, version_id) VALUES (999, 999, 999)",
        "INSERT INTO knowledge (current_version_id) VALUES (999)",
        "INSERT INTO knowledge (indexed_version_id) VALUES (999)",
        "INSERT INTO knowledge (indexed_embedding_model_id) VALUES (999)",
    ])
    def test_foreign_keys_reject_missing_targets(self, connection, sql):
        """验证 `foreign_keys_reject_missing_targets` 所代表的行为。 参数包括：`connection`、`sql`。"""
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(sql)

    def test_document_chunk_version_source_and_category_uniqueness(self, connection):
        """验证 `document_chunk_version_source_and_category_uniqueness` 所代表的行为。 参数包括：`connection`。"""
        document_id = connection.execute(
            "INSERT INTO source_document (title, storage_url, content_hash) VALUES ('文档', 'file', 'h')"
        ).lastrowid
        # 正文哈希与切片哈希均不是全局唯一键。
        connection.execute("INSERT INTO source_document (title, storage_url, content_hash) VALUES ('同文', 'file2', 'h')")
        chunk_sql = "INSERT INTO document_chunk (source_document_id, chunk_batch, chunk_index, content, chunk_hash) VALUES (?, ?, 0, '原文', 'h')"
        chunk_id = connection.execute(chunk_sql, (document_id, 1)).lastrowid
        connection.execute(chunk_sql, (document_id, 2))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(chunk_sql, (document_id, 1))
        knowledge_id, _ = _knowledge(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO knowledge_version (knowledge_id, version_no, content, content_hash, change_type) VALUES (?, 1, '新正文', 'h2', 2)",
                (knowledge_id,),
            )
        source_sql = "INSERT INTO knowledge_source (knowledge_id, chunk_id, source_role) VALUES (?, ?, ?)"
        connection.execute(source_sql, (knowledge_id, chunk_id, 0))
        connection.execute(source_sql, (knowledge_id, chunk_id, 1))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(source_sql, (knowledge_id, chunk_id, 0))
        category_id = connection.execute("INSERT INTO category (name) VALUES ('分类')").lastrowid
        category_sql = "INSERT INTO knowledge_category (knowledge_id, category_id) VALUES (?, ?)"
        connection.execute(category_sql, (knowledge_id, category_id))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(category_sql, (knowledge_id, category_id))
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute("DELETE FROM source_document WHERE id = ?", (document_id,))

    def test_business_polymorphic_checks_are_not_schema_foreign_keys(self, connection):
        # 仅核对 SQL 保护边界；业务校验随 T1.5/T3.6 的写入服务实现。
        """验证 `business_polymorphic_checks_are_not_schema_foreign_keys` 所代表的行为。 参数包括：`connection`。"""
        assert connection.execute("PRAGMA foreign_key_list(alias)").fetchall() == []
        version_fk_columns = {row[3] for row in connection.execute("PRAGMA foreign_key_list(knowledge_version)")}
        assert version_fk_columns == {"knowledge_id"}

    def test_queue_index_columns(self, connection):
        """验证 `queue_index_columns` 所代表的行为。 参数包括：`connection`。"""
        assert [row[2] for row in connection.execute("PRAGMA index_info(idx_task_process_queue)")] == ["task_type", "status"]


class TestTransactions:
    def test_knowledge_version_alias_fts_and_task_rollback_together(self, connection):
        """验证 `knowledge_version_alias_fts_and_task_rollback_together` 所代表的行为。 参数包括：`connection`。"""
        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                knowledge_id, version_id = _knowledge(connection)
                connection.execute("INSERT INTO category (name) VALUES ('分类')")
                connection.execute("INSERT INTO alias (owner_type, owner_id, name) VALUES (0, ?, 'AI')", (knowledge_id,))
                connection.execute(
                    "INSERT INTO knowledge_fts (rowid, content_tokens, knowledge_id, version_id) VALUES (?, '完整 正文', ?, ?)",
                    (knowledge_id, knowledge_id, version_id),
                )
                connection.execute(
                    "INSERT INTO task_process (task_type, knowledge_id, version_id, operation) VALUES (1, ?, ?, 0)",
                    (knowledge_id, version_id),
                )
                connection.execute("INSERT INTO alias (owner_type, owner_id, name, origin) VALUES (0, ?, 'a i', 1)", (knowledge_id,))
        for table in ("knowledge", "knowledge_version", "category", "alias", "knowledge_fts", "task_process"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

    def test_initial_knowledge_version_pointer_survives_commit(self, database_path):
        """验证 `initial_knowledge_version_pointer_survives_commit` 所代表的行为。 参数包括：`database_path`。"""
        with closing(connect_database(database_path)) as database, database:
            knowledge_id, version_id = _knowledge(database)
        with closing(connect_database(database_path)) as database:
            assert database.execute(
                "SELECT k.current_version_id, v.knowledge_id FROM knowledge k JOIN "
                "knowledge_version v ON v.id = k.current_version_id WHERE k.id = ?", (knowledge_id,)
            ).fetchone() == (version_id, knowledge_id)
            assert database.execute("PRAGMA foreign_key_check").fetchall() == []
