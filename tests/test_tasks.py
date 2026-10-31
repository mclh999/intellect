"""T3.1：既定任务类型条件必填、状态组合及只读队列查询。"""

from contextlib import closing

import pytest

from intellect.core.errors import TaskExecutionError
from intellect.core.tasks import TaskQueryService
from intellect.db import connect_database, initialize_database
from intellect.doc.batches import DocumentBatchService


@pytest.fixture
def connection(tmp_path):
    """执行 `connection` 对应的功能并返回处理结果。 参数包括：`tmp_path`。"""
    path = tmp_path / "tasks.db"
    initialize_database(path)
    with closing(connect_database(path)) as connection:
        yield connection


def import_task(connection):
    """执行 `import_task` 对应的功能并返回处理结果。 参数包括：`connection`。"""
    with connection:
        document = connection.execute(
            "INSERT INTO source_document (title,storage_url,content_hash) VALUES ('资料','file:///source','test')"
        ).lastrowid
    return DocumentBatchService(connection).create(document, parser="test", parser_version="1").task_id


def sync_task(connection, operation=0):
    """执行 `sync_task` 对应的功能并返回处理结果。 参数包括：`connection`、`operation`。"""
    with connection:
        knowledge = connection.execute("INSERT INTO knowledge DEFAULT VALUES").lastrowid
        version = connection.execute(
            "INSERT INTO knowledge_version (knowledge_id,version_no,content,content_hash,change_type) "
            "VALUES (?,1,'完整知识','test',0)", (knowledge,),
        ).lastrowid
        connection.execute("UPDATE knowledge SET current_version_id=? WHERE id=?", (version, knowledge))
        return connection.execute(
            "INSERT INTO task_process (task_type,knowledge_id,version_id,operation) VALUES (1,?,?,?)",
            (knowledge, version, operation),
        ).lastrowid


class TestTaskQueries:
    def test_import_query_preserves_records_and_transaction_state(self, connection):
        """验证 `import_query_preserves_records_and_transaction_state` 所代表的行为。 参数包括：`connection`。"""
        task_id = import_task(connection)
        before = connection.total_changes
        record = TaskQueryService(connection).get(task_id)
        assert record.status == 0 and record.chunk_count is None
        assert record.task_type == 0 and record.retry_count == 0
        assert connection.total_changes == before and not connection.in_transaction

    @pytest.mark.parametrize("operation", [0, 1])
    def test_sync_fields_and_historical_target_versions_are_supported(self, connection, operation):
        """验证 `sync_fields_and_historical_target_versions_are_supported` 所代表的行为。 参数包括：`connection`、`operation`。"""
        task_id = sync_task(connection, operation)
        record = TaskQueryService(connection).get(task_id)
        with connection:
            new_version = connection.execute(
                "INSERT INTO knowledge_version (knowledge_id,version_no,content,content_hash,change_type) "
                "VALUES (?,2,'新版本','new',1)", (record.knowledge_id,),
            ).lastrowid
            connection.execute("UPDATE knowledge SET current_version_id=? WHERE id=?", (new_version, record.knowledge_id))
        assert TaskQueryService(connection).get(task_id) == record

    def test_pending_queries_keep_types_separate_without_claiming(self, connection):
        """验证 `pending_queries_keep_types_separate_without_claiming` 所代表的行为。 参数包括：`connection`。"""
        first, second = import_task(connection), import_task(connection)
        sync = sync_task(connection)
        with connection:
            connection.execute("UPDATE task_process SET status=4 WHERE id=?", (second,))
        service = TaskQueryService(connection)
        assert [task.id for task in service.list_pending(0)] == [first]
        assert [task.id for task in service.list_pending(1)] == [sync]
        assert connection.execute("SELECT status FROM task_process WHERE id=?", (first,)).fetchone() == (0,)

    @pytest.mark.parametrize("task_id", [0, -1, True, "1", 999])
    def test_invalid_or_missing_task_id_rejected(self, connection, task_id):
        """验证 `invalid_or_missing_task_id_rejected` 所代表的行为。 参数包括：`connection`、`task_id`。"""
        with pytest.raises(TaskExecutionError):
            TaskQueryService(connection).get(task_id)

    @pytest.mark.parametrize("kind", [-1, True, 2, "0"])
    def test_unknown_task_types_rejected(self, connection, kind):
        """验证 `unknown_task_types_rejected` 所代表的行为。 参数包括：`connection`、`kind`。"""
        with pytest.raises(TaskExecutionError):
            TaskQueryService(connection).list_pending(kind)

    def test_query_does_not_commit_outer_transaction(self, connection):
        """验证 `query_does_not_commit_outer_transaction` 所代表的行为。 参数包括：`connection`。"""
        task_id = import_task(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE task_process SET status=1 WHERE id=?", (task_id,))
        assert TaskQueryService(connection).get(task_id).status == 1
        assert connection.in_transaction
        connection.rollback()
        assert TaskQueryService(connection).get(task_id).status == 0

    def test_queue_and_field_validation_share_read_snapshot(self, connection, monkeypatch):
        """验证 `queue_and_field_validation_share_read_snapshot` 所代表的行为。 参数包括：`connection`、`monkeypatch`。"""
        task_id = import_task(connection)
        database_path = connection.execute("PRAGMA database_list").fetchone()[2]
        service = TaskQueryService(connection)
        get = service.get
        def change_then_get(task_id):
            """执行 `change_then_get` 对应的功能并返回处理结果。 参数包括：`task_id`。"""
            with closing(connect_database(database_path)) as writer, writer:
                writer.execute("UPDATE task_process SET status=1 WHERE id=?", (task_id,))
            return get(task_id)
        monkeypatch.setattr(service, "get", change_then_get)
        assert [task.status for task in service.list_pending(0)] == [0]
        assert get(task_id).status == 1 and not connection.in_transaction


class TestTaskFieldValidation:
    @pytest.mark.parametrize("field,value", [
        ("task_type", 2), ("status", 5), ("retry_count", -1), ("retry_count", 0.5),
        ("source_document_id", None), ("chunk_batch", None), ("parse_config", None),
        ("parse_config", "[]"), ("parse_config", "invalid"), ("chunk_count", 0.5),
        ("extract_config", "[]"), ("operation", 0),
    ])
    def test_invalid_import_field_combinations_rejected(self, connection, field, value):
        """验证 `invalid_import_field_combinations_rejected` 所代表的行为。 参数包括：`connection`、`field`、`value`。"""
        task_id = import_task(connection)
        with connection:
            connection.execute(f"UPDATE task_process SET {field}=? WHERE id=?", (value, task_id))
        with pytest.raises(TaskExecutionError):
            TaskQueryService(connection).get(task_id)

    @pytest.mark.parametrize("field,value", [
        ("status", 2), ("knowledge_id", None), ("version_id", None), ("operation", None),
        ("operation", 2), ("chunk_batch", 1), ("parse_config", "{}"),
        ("chunk_count", 0), ("extract_config", "{}"),
    ])
    def test_invalid_sync_field_combinations_rejected(self, connection, field, value):
        """验证 `invalid_sync_field_combinations_rejected` 所代表的行为。 参数包括：`connection`、`field`、`value`。"""
        task_id = sync_task(connection)
        with connection:
            connection.execute(f"UPDATE task_process SET {field}=? WHERE id=?", (value, task_id))
        with pytest.raises(TaskExecutionError):
            TaskQueryService(connection).get(task_id)

    def test_sync_version_owner_must_match_knowledge(self, connection):
        """验证 `sync_version_owner_must_match_knowledge` 所代表的行为。 参数包括：`connection`。"""
        first, second = sync_task(connection), sync_task(connection)
        other = TaskQueryService(connection).get(second)
        with connection:
            connection.execute("UPDATE task_process SET version_id=? WHERE id=?", (other.version_id, first))
        with pytest.raises(TaskExecutionError, match="不属于"):
            TaskQueryService(connection).get(first)
