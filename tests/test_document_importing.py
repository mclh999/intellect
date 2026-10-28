"""T3.2：正文优先去重、显式确认、并发复核与登记事务。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from intellect.db import connect_database, initialize_database
from intellect.doc import importing
from intellect.doc.hashing import document_hash, file_hash
from intellect.doc.importing import DocumentImportError, DocumentImportService, DuplicateDocumentError
from intellect.doc.processing import DocumentProcessingError


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "imports.db"
    initialize_database(path)
    return path


@pytest.fixture
def connection(database_path):
    with closing(connect_database(database_path)) as connection:
        yield connection


def archive(tmp_path, name="first.txt", data=b"Alpha\r\nBeta"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


class TestFileDeduplication:
    def test_truthy_text_cannot_replace_explicit_confirmation(self, connection, tmp_path):
        path = archive(tmp_path)
        service = DocumentImportService(connection)
        service.register(path, title="原件", kind="TXT")
        with pytest.raises(DocumentImportError):
            service.register(path, title="重复", kind="TXT", confirmed_duplicate="true")
        assert connection.execute("SELECT count(*) FROM source_document").fetchone()[0] == 1

    def test_same_file_default_rejected_without_new_records(self, connection, tmp_path):
        path = archive(tmp_path)
        service = DocumentImportService(connection)
        first = service.register(path, title="原文档", kind="TXT")
        with pytest.raises(DuplicateDocumentError) as caught:
            service.register(path, title="重复上传", kind="TXT")
        assert caught.value.code == "DUPLICATE_DOCUMENT"
        assert caught.value.context["match"] == "file"
        assert caught.value.context["documents"][0]["id"] == first.document_id
        assert caught.value.context["documents"][0]["title"] == "原文档"
        assert connection.execute("SELECT count(*) FROM source_document").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM task_process").fetchone()[0] == 1

    def test_same_body_different_file_default_rejected(self, connection, tmp_path):
        service = DocumentImportService(connection)
        first = service.register(archive(tmp_path), title="CRLF", kind="TXT")
        other = archive(tmp_path, "second.txt", b"Alpha\nBeta")
        with pytest.raises(DuplicateDocumentError) as caught:
            service.register(other, title="LF", kind="TXT")
        assert caught.value.context["match"] == "content"
        assert caught.value.context["documents"][0]["id"] == first.document_id
        assert not caught.value.context["documents"][0]["same_file"]

    def test_explicit_confirmation_preserves_independent_sources(self, connection, tmp_path):
        service = DocumentImportService(connection)
        first = service.register(archive(tmp_path), title="原来源", kind="TXT")
        second_path = archive(tmp_path, "second.txt", b"Alpha\nBeta")
        second = service.register(second_path, title="独立来源", kind="TXT", confirmed_duplicate=True)
        assert second.document_id != first.document_id
        assert second.batch.task_id != first.batch.task_id
        assert [match.document_id for match in second.duplicate_matches] == [first.document_id]
        service.processing.run(first.batch.task_id)
        service.processing.run(second.batch.task_id)
        chunks = connection.execute("SELECT id,source_document_id FROM document_chunk ORDER BY id").fetchall()
        assert len(chunks) == 2 and len({row[0] for row in chunks}) == 2
        assert {row[1] for row in chunks} == {first.document_id, second.document_id}
        assert connection.execute("SELECT count(DISTINCT content_hash) FROM source_document").fetchone()[0] == 1

    def test_inactive_documents_do_not_block_new_source(self, connection, tmp_path):
        path = archive(tmp_path)
        service = DocumentImportService(connection)
        first = service.register(path, title="旧来源", kind="TXT")
        with connection:
            connection.execute("UPDATE source_document SET status=1 WHERE id=?", (first.document_id,))
        second = service.register(path, title="新来源", kind="TXT")
        assert second.document_id != first.document_id
        assert second.duplicate_matches == ()

    def test_file_hash_compared_after_body_match_not_as_global_shortcut(self, connection, tmp_path):
        # 相同原始字节用不同解析器产生不同正文；按 §5.1 正文未命中正常登记。
        path = archive(tmp_path, "page.html", b"<p>Alpha</p>")
        service = DocumentImportService(connection)
        html = service.register(path, title="HTML", kind="WEB")
        text = service.register(path, title="源码文本", kind="TXT")
        assert html.document_id != text.document_id and text.duplicate_matches == ()
        assert connection.execute("SELECT count(DISTINCT file_hash),count(DISTINCT content_hash) FROM source_document").fetchone() == (1, 2)

    def test_all_matching_active_documents_are_shown(self, connection, tmp_path):
        path = archive(tmp_path)
        service = DocumentImportService(connection)
        one = service.register(path, title="一", kind="TXT")
        two = service.register(path, title="二", kind="TXT", confirmed_duplicate=True)
        with pytest.raises(DuplicateDocumentError) as caught:
            service.register(path, title="三", kind="TXT")
        assert [row["id"] for row in caught.value.context["documents"]] == [one.document_id, two.document_id]

    def test_empty_parsed_body_still_uses_content_deduplication(self, connection, tmp_path):
        path = archive(tmp_path, data=b"")
        service = DocumentImportService(connection)
        service.register(path, title="空文档", kind="TXT")
        with pytest.raises(DuplicateDocumentError):
            service.register(path, title="重复空文档", kind="TXT")


class TestRegistrationTransaction:
    def test_explicit_encoding_is_shared_by_inspection_and_task_snapshot(self, connection, tmp_path):
        path = archive(tmp_path, data="中文归档".encode("gb18030"))
        service = DocumentImportService(connection)
        result = service.register(path, title="GB 编码", kind="TXT", parser_options={"encoding":"gb18030"})
        service.processing.run(result.batch.task_id)
        assert connection.execute("SELECT content FROM document_chunk").fetchone()[0] == "中文归档"
        assert connection.execute("SELECT content_hash FROM source_document").fetchone()[0] == document_hash("中文归档")

    def test_metadata_and_snapshot_registered_before_chunks(self, connection, tmp_path):
        path = archive(tmp_path)
        result = DocumentImportService(connection).register(path, title="文档", kind="TXT", uploader="system")
        assert connection.execute("SELECT storage_url,file_hash,content_hash,file_size,uploader_id,chunk_batch FROM source_document").fetchone() == (
            path.as_uri(), file_hash(path.read_bytes()), document_hash("Alpha\r\nBeta"), len(path.read_bytes()), "system", None,
        )
        assert result.batch.chunk_count is None
        assert connection.execute("SELECT count(*) FROM document_chunk").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0

    def test_batch_creation_failure_rolls_back_document(self, connection, tmp_path, monkeypatch):
        service = DocumentImportService(connection)
        def fail(*args, **kwargs):
            raise RuntimeError("准备批次失败")
        monkeypatch.setattr(service.processing, "create", fail)
        with pytest.raises(RuntimeError):
            service.register(archive(tmp_path), title="失败", kind="TXT")
        assert connection.execute("SELECT count(*) FROM source_document").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM task_process").fetchone()[0] == 0

    def test_archive_changed_between_inspection_and_registration_rolls_back(self, connection, tmp_path, monkeypatch):
        path = archive(tmp_path)
        real_inspect = importing.inspect_bytes
        def inspect_then_change(*args, **kwargs):
            result = real_inspect(*args, **kwargs)
            path.write_bytes(b"changed")
            return result
        monkeypatch.setattr(importing, "inspect_bytes", inspect_then_change)
        with pytest.raises(DocumentProcessingError, match="哈希不一致"):
            DocumentImportService(connection).register(path, title="已变化", kind="TXT")
        assert connection.execute("SELECT count(*) FROM source_document").fetchone()[0] == 0

    def test_outer_rollback_includes_document_and_task(self, connection, tmp_path, database_path):
        connection.execute("BEGIN IMMEDIATE")
        DocumentImportService(connection).register(archive(tmp_path), title="外层", kind="TXT")
        assert connection.in_transaction
        with closing(connect_database(database_path)) as reader:
            assert reader.execute("SELECT count(*) FROM source_document").fetchone()[0] == 0
        connection.rollback()
        assert connection.execute("SELECT count(*) FROM source_document").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM task_process").fetchone()[0] == 0

    def test_concurrent_identical_files_only_register_once_without_confirmation(self, database_path, tmp_path):
        path = archive(tmp_path)
        barrier = Barrier(2)
        def register():
            with closing(connect_database(database_path)) as connection:
                barrier.wait(timeout=10)
                try:
                    return DocumentImportService(connection).register(path, title="并发", kind="TXT").document_id
                except DuplicateDocumentError:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: register(), range(2)))
        assert sum(value is not None for value in results) == 1
        with closing(connect_database(database_path)) as connection:
            assert connection.execute("SELECT count(*) FROM source_document").fetchone()[0] == 1
