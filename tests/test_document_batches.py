"""T2.8：批次隔离、失败恢复、完整发布与事务组合。"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from types import SimpleNamespace

import pytest

from intellect.core.config import ChunkConfig
from intellect.db import connect_database, initialize_database
from intellect.doc import batches as batches_module
from intellect.doc import hashing
from intellect.doc.batches import ChunkInput, DocumentBatchError, DocumentBatchService
from intellect.doc.hashing import ChunkHashError, chunk_hash


@pytest.fixture
def database_path(tmp_path):
    """执行 `database_path` 对应的功能并返回处理结果。 参数包括：`tmp_path`。"""
    path = tmp_path / "batches.db"
    initialize_database(path)
    return path


@pytest.fixture
def connection(database_path):
    """执行 `connection` 对应的功能并返回处理结果。 参数包括：`database_path`。"""
    with closing(connect_database(database_path)) as connection:
        with connection:
            connection.execute("INSERT INTO source_document (title, storage_url, content_hash) VALUES ('原件', 'file', 'test')")
        yield connection


def create(connection, document_id=1, **kwargs):
    """执行 `create` 对应的功能并返回处理结果。 参数包括：`connection`、`document_id`、`**kwargs`。"""
    return DocumentBatchService(connection).create(
        document_id, parser="test-parser", parser_version="1.0",
        chunking=ChunkConfig(chunk_size=80, chunk_overlap=8), **kwargs,
    )


class TestChunkHash:
    def test_known_utf8_digest_and_no_normalization(self):
        """验证 `known_utf8_digest_and_no_normalization` 所代表的行为。 参数包括：无显式参数。"""
        assert chunk_hash("abc") == "chunk-v1:sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        assert chunk_hash("") == "chunk-v1:sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        for left, right in [("a\r\nb", "a\nb"), ("可以", "不可以"), ("10 mg", "100 mg"),
                            ("ABC", "abc"), ("é", "e\u0301"), ("a b", "ab")]:
            assert chunk_hash(left) != chunk_hash(right)

    def test_unencodable_text_fails(self):
        """验证 `unencodable_text_fails` 所代表的行为。 参数包括：无显式参数。"""
        with pytest.raises(ChunkHashError):
            chunk_hash("\ud800")

    def test_manifest_mismatch_is_not_silently_accepted(self, tmp_path, monkeypatch):
        """验证 `manifest_mismatch_is_not_silently_accepted` 所代表的行为。 参数包括：`tmp_path`、`monkeypatch`。"""
        (tmp_path / "hash_protocols.json").write_text('{"chunk-v1": {}}', encoding="utf-8")
        hashing._prefix.cache_clear()
        monkeypatch.setattr(hashing, "files", lambda package: tmp_path)
        try:
            with pytest.raises(ChunkHashError):
                chunk_hash("abc")
        finally:
            hashing._prefix.cache_clear()


class TestBatchAllocation:
    def test_first_batch_and_snapshot_are_persisted_before_chunks(self, connection, monkeypatch):
        """验证 `first_batch_and_snapshot_are_persisted_before_chunks` 所代表的行为。 参数包括：`connection`、`monkeypatch`。"""
        settings = SimpleNamespace(chunk=ChunkConfig(chunk_size=90, chunk_overlap=9))
        monkeypatch.setattr(batches_module, "get_settings", lambda: settings)
        options = {"encoding": "utf-8"}
        service = DocumentBatchService(connection)
        batch = service.create(1, parser="txt", parser_version="1", parser_options=options)
        options["encoding"] = "gbk"
        settings.chunk = ChunkConfig()
        restored = service.get(batch.task_id)
        assert restored == batch
        snapshot = json.loads(restored.parse_config)
        assert snapshot["chunk_size"] == 90
        assert snapshot["parser_options"] == {"encoding": "utf-8"}
        assert batch.chunk_batch == 1 and batch.chunk_count is None
        assert connection.execute("SELECT chunk_batch, parse_config FROM source_document").fetchone() == (None, None)

    def test_zero_written_failed_task_keeps_batch_reserved(self, connection):
        """验证 `zero_written_failed_task_keeps_batch_reserved` 所代表的行为。 参数包括：`connection`。"""
        batch = create(connection)
        with connection:
            connection.execute("UPDATE task_process SET status=4 WHERE id=?", (batch.task_id,))
        service = DocumentBatchService(connection)
        with pytest.raises(DocumentBatchError, match="重试原任务") as error:
            create(connection)
        assert error.value.context == {"task_id": batch.task_id}
        assert service.get(batch.task_id) == batch
        service.save_chunks(batch.task_id, [ChunkInput(0, "重试")])
        assert service.publish(batch.task_id, expected_count=1).chunk_batch == 1

    def test_different_documents_can_generate_independently(self, connection):
        """验证 `different_documents_can_generate_independently` 所代表的行为。 参数包括：`connection`。"""
        with connection:
            doc = connection.execute("INSERT INTO source_document (title, storage_url, content_hash) VALUES ('另一原件','file2','test')").lastrowid
        assert create(connection).chunk_batch == create(connection, doc).chunk_batch == 1

    def test_maximum_uses_existing_history_not_only_current_pointer(self, connection):
        """验证 `maximum_uses_existing_history_not_only_current_pointer` 所代表的行为。 参数包括：`connection`。"""
        with connection:
            connection.execute("INSERT INTO document_chunk (source_document_id,chunk_batch,chunk_index,content,chunk_hash) VALUES (1,7,0,'历史','h')")
        assert create(connection).chunk_batch == 8

    def test_two_connections_cannot_allocate_two_unpublished_batches(self, connection, database_path):
        """验证 `two_connections_cannot_allocate_two_unpublished_batches` 所代表的行为。 参数包括：`connection`、`database_path`。"""
        barrier = Barrier(2)
        def allocate():
            """执行 `allocate` 对应的功能并返回处理结果。 参数包括：无显式参数。"""
            with closing(connect_database(database_path)) as db:
                barrier.wait(timeout=10)
                try:
                    return create(db).task_id
                except DocumentBatchError:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: allocate(), range(2)))
        assert sum(result is not None for result in results) == 1
        assert connection.execute("SELECT COUNT(*) FROM task_process").fetchone()[0] == 1

    def test_allocation_participates_in_outer_transaction(self, connection):
        """验证 `allocation_participates_in_outer_transaction` 所代表的行为。 参数包括：`connection`。"""
        connection.execute("BEGIN IMMEDIATE")
        batch = create(connection)
        assert batch.chunk_batch == 1 and connection.in_transaction
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM task_process").fetchone()[0] == 0

    def test_missing_or_disabled_document_and_sync_task_rejected(self, connection):
        """验证 `missing_or_disabled_document_and_sync_task_rejected` 所代表的行为。 参数包括：`connection`。"""
        with pytest.raises(DocumentBatchError):
            create(connection, 999)
        with connection:
            connection.execute("UPDATE source_document SET status=1 WHERE id=1")
            task = connection.execute("INSERT INTO task_process(task_type) VALUES(1)").lastrowid
        with pytest.raises(DocumentBatchError):
            create(connection)
        with pytest.raises(DocumentBatchError):
            DocumentBatchService(connection).get(task)

    def test_old_schema_is_rejected_without_migration(self):
        """验证 `old_schema_is_rejected_without_migration` 所代表的行为。 参数包括：无显式参数。"""
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE task_process(id INTEGER PRIMARY KEY, task_type INTEGER)")
            with pytest.raises(DocumentBatchError, match="V0.7"):
                DocumentBatchService(db)
            assert len(db.execute("PRAGMA table_info(task_process)").fetchall()) == 2

    def test_schema_guards_duplicate_batches_and_unpublished_slots(self, connection):
        """验证 `schema_guards_duplicate_batches_and_unpublished_slots` 所代表的行为。 参数包括：`connection`。"""
        batch = create(connection)
        with pytest.raises(sqlite3.IntegrityError), connection:
            connection.execute(
                "INSERT INTO task_process(task_type,source_document_id,chunk_batch) VALUES(0,1,2)"
            )
        DocumentBatchService(connection).publish(batch.task_id, expected_count=0)
        with pytest.raises(sqlite3.IntegrityError), connection:
            connection.execute(
                "INSERT INTO task_process(task_type,source_document_id,chunk_batch,chunk_count) VALUES(0,1,1,0)"
            )
        with connection:
            connection.execute("INSERT INTO task_process(task_type) VALUES(1)")
            connection.execute("INSERT INTO task_process(task_type) VALUES(1)")
        assert connection.execute("SELECT COUNT(*) FROM task_process WHERE task_type=1").fetchone()[0] == 2

    @pytest.mark.parametrize("value", [[], {"bad": float("nan")}, {"bad": object()}])
    def test_invalid_options_do_not_create_task(self, connection, value):
        """验证 `invalid_options_do_not_create_task` 所代表的行为。 参数包括：`connection`、`value`。"""
        with pytest.raises(DocumentBatchError):
            create(connection, parser_options=value)
        assert connection.execute("SELECT COUNT(*) FROM task_process").fetchone()[0] == 0


class TestChunkResume:
    def test_resume_reuses_ids_and_only_adds_missing_chunks(self, connection):
        """验证 `resume_reuses_ids_and_only_adds_missing_chunks` 所代表的行为。 参数包括：`connection`。"""
        service = DocumentBatchService(connection)
        batch = create(connection)
        first = ChunkInput(0, "原文\r\n", 3, {"page": 1})
        old_ids = service.save_chunks(batch.task_id, [first])
        ids = service.save_chunks(batch.task_id, [first, ChunkInput(1, "后续", 2)])
        assert ids[:1] == old_ids
        assert connection.execute("SELECT COUNT(*) FROM document_chunk").fetchone()[0] == 2
        assert connection.execute("SELECT content FROM document_chunk WHERE id=?", old_ids).fetchone()[0] == first.content

    @pytest.mark.parametrize("changed", [ChunkInput(0,"不同"), ChunkInput(0,"原文",9), ChunkInput(0,"原文",None,{"page":2})])
    def test_conflicting_retry_rolls_back_new_rows_without_changing_evidence(self, connection, changed):
        """验证 `conflicting_retry_rolls_back_new_rows_without_changing_evidence` 所代表的行为。 参数包括：`connection`、`changed`。"""
        service = DocumentBatchService(connection)
        batch = create(connection)
        service.save_chunks(batch.task_id, [ChunkInput(0, "原文")])
        with pytest.raises(DocumentBatchError):
            service.save_chunks(batch.task_id, [ChunkInput(1, "本次新增"), changed])
        assert connection.execute("SELECT chunk_index,content FROM document_chunk").fetchall() == [(0,"原文")]

    def test_generator_failure_keeps_previous_commits_only(self, connection):
        """验证 `generator_failure_keeps_previous_commits_only` 所代表的行为。 参数包括：`connection`。"""
        service = DocumentBatchService(connection)
        batch = create(connection)
        service.save_chunks(batch.task_id, [ChunkInput(0, "已提交")])
        def failing():
            """执行 `failing` 对应的功能并返回处理结果。 参数包括：无显式参数。"""
            yield ChunkInput(1, "本次暂存")
            raise RuntimeError("解析中断")
        with pytest.raises(RuntimeError):
            service.save_chunks(batch.task_id, failing())
        assert connection.execute("SELECT content FROM document_chunk").fetchall() == [("已提交",)]

    def test_failed_nested_call_keeps_callers_uncommitted_work(self, connection):
        """验证 `failed_nested_call_keeps_callers_uncommitted_work` 所代表的行为。 参数包括：`connection`。"""
        service = DocumentBatchService(connection)
        batch = create(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE source_document SET title='外层事务' WHERE id=1")
        with pytest.raises(DocumentBatchError):
            service.save_chunks(batch.task_id, [ChunkInput(0,"暂存"), ChunkInput(-1,"错误")])
        assert connection.in_transaction
        assert connection.execute("SELECT title FROM source_document").fetchone()[0] == "外层事务"
        assert connection.execute("SELECT COUNT(*) FROM document_chunk").fetchone()[0] == 0
        connection.rollback()

    def test_equal_content_in_other_batch_is_separate_evidence(self, connection):
        """验证 `equal_content_in_other_batch_is_separate_evidence` 所代表的行为。 参数包括：`connection`。"""
        service = DocumentBatchService(connection)
        first = create(connection)
        old_id = service.save_chunks(first.task_id, [ChunkInput(0,"相同")])[0]
        service.publish(first.task_id, expected_count=1)
        second = create(connection)
        new_id = service.save_chunks(second.task_id, [ChunkInput(0,"相同")])[0]
        assert new_id != old_id
        assert connection.execute("SELECT chunk_batch FROM source_document").fetchone()[0] == 1


class TestBatchPublication:
    @pytest.mark.parametrize("chunks,count", [([ChunkInput(1,"缺0")],1), ([ChunkInput(0,"不完整")],2)])
    def test_incomplete_batch_does_not_publish(self, connection, chunks, count):
        """验证 `incomplete_batch_does_not_publish` 所代表的行为。 参数包括：`connection`、`chunks`、`count`。"""
        service = DocumentBatchService(connection)
        batch = create(connection)
        service.save_chunks(batch.task_id, chunks)
        with pytest.raises(DocumentBatchError):
            service.publish(batch.task_id, expected_count=count)
        assert service.get(batch.task_id).chunk_count is None
        assert connection.execute("SELECT chunk_batch FROM source_document").fetchone()[0] is None

    def test_successful_empty_batch_differs_from_unfinished(self, connection):
        """验证 `successful_empty_batch_differs_from_unfinished` 所代表的行为。 参数包括：`connection`。"""
        service = DocumentBatchService(connection)
        batch = create(connection)
        published = service.publish(batch.task_id, expected_count=0)
        assert published.chunk_count == 0
        assert connection.execute("SELECT chunk_batch,parse_config FROM source_document").fetchone() == (1,batch.parse_config)
        assert connection.execute("SELECT status FROM task_process").fetchone()[0] == 0
        assert create(connection).chunk_batch == 2

    def test_old_publication_retry_cannot_rewind_pointer_or_change_snapshot(self, connection):
        """验证 `old_publication_retry_cannot_rewind_pointer_or_change_snapshot` 所代表的行为。 参数包括：`connection`。"""
        service = DocumentBatchService(connection)
        first = create(connection, parser_options={"mode":"old"})
        service.publish(first.task_id, expected_count=0)
        second = create(connection, parser_options={"mode":"new"})
        service.publish(second.task_id, expected_count=0)
        service.publish(first.task_id, expected_count=0)
        assert connection.execute("SELECT chunk_batch,parse_config FROM source_document").fetchone() == (2,second.parse_config)
        assert service.get(first.task_id).parse_config == first.parse_config
        with pytest.raises(DocumentBatchError):
            service.save_chunks(first.task_id,[ChunkInput(0,"迟到切片")])
        with pytest.raises(DocumentBatchError):
            service.publish(first.task_id,expected_count=1)

    def test_publish_failure_rolls_back_marker_and_keeps_previous_batch(self, connection):
        """验证 `publish_failure_rolls_back_marker_and_keeps_previous_batch` 所代表的行为。 参数包括：`connection`。"""
        service = DocumentBatchService(connection)
        old = create(connection)
        service.publish(old.task_id, expected_count=0)
        new = create(connection)
        service.save_chunks(new.task_id, [ChunkInput(0,"新片段")])
        connection.execute("CREATE TRIGGER fail_publish BEFORE UPDATE ON source_document BEGIN SELECT RAISE(ABORT,'模拟发布失败'); END")
        with pytest.raises(sqlite3.IntegrityError):
            service.publish(new.task_id, expected_count=1)
        assert service.get(new.task_id).chunk_count is None
        assert connection.execute("SELECT chunk_batch,parse_config FROM source_document").fetchone() == (1,old.parse_config)
        assert connection.execute("SELECT COUNT(*) FROM document_chunk").fetchone()[0] == 1

    def test_hash_corruption_or_different_protocol_prevents_publish(self, connection):
        """验证 `hash_corruption_or_different_protocol_prevents_publish` 所代表的行为。 参数包括：`connection`。"""
        service = DocumentBatchService(connection)
        batch = create(connection)
        service.save_chunks(batch.task_id,[ChunkInput(0,"原文")])
        with connection:
            connection.execute("UPDATE document_chunk SET chunk_hash=replace(chunk_hash,'chunk-v1','chunk-v2')")
        with pytest.raises(DocumentBatchError):
            service.publish(batch.task_id,expected_count=1)

    def test_publication_and_chunks_respect_outer_rollback(self, connection):
        """验证 `publication_and_chunks_respect_outer_rollback` 所代表的行为。 参数包括：`connection`。"""
        service = DocumentBatchService(connection)
        batch = create(connection)
        connection.execute("BEGIN IMMEDIATE")
        service.save_chunks(batch.task_id,[ChunkInput(0,"原文")])
        service.publish(batch.task_id,expected_count=1)
        assert connection.in_transaction
        connection.rollback()
        assert service.get(batch.task_id).chunk_count is None
        assert connection.execute("SELECT COUNT(*) FROM document_chunk").fetchone()[0] == 0
