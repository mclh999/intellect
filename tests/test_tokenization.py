"""T2.3：共用分词、资产版本、配置快照及词典升级后的原子重建。"""

from __future__ import annotations

import hashlib
from contextlib import closing
from dataclasses import asdict

import jieba
import pytest

from intellect.core.config import Settings, TokenizationConfig
from intellect.db import connect_database, initialize_database
from intellect.index.fts import rebuild_fts
from intellect.knowledge import AliasService, CategoryService, KnowledgeManagementError
from intellect.knowledge.fts import refresh_knowledge
from intellect.search import tokenization as tokenization_module
from intellect.search.tokenization import (
    TextTokenizer, TokenizationError, get_tokenizer, reset_tokenizer_cache, token_text, tokenize,
)


@pytest.fixture
def connection(tmp_path):
    path = tmp_path / "tokenization.db"
    initialize_database(path)
    with closing(connect_database(path)) as database:
        yield database


def add_knowledge(connection, text, status=0):
    with connection:
        kid = connection.execute("INSERT INTO knowledge (status) VALUES (?)", (status,)).lastrowid
        vid = connection.execute(
            "INSERT INTO knowledge_version (knowledge_id, version_no, title, content, content_hash, change_type) "
            "VALUES (?, 1, '标题', ?, 'fixture-hash', 0)", (kid, text),
        ).lastrowid
        connection.execute("UPDATE knowledge SET current_version_id = ? WHERE id = ?", (vid, kid))
        refresh_knowledge(connection, kid)
    return kid


class TestSharedTokenization:
    @pytest.mark.parametrize("text", ["原子性与可见性", "ＡＩ AI A I", "Alpha-Beta OR C++", "否定不等于无条件", " \t\n"])
    def test_default_tokens_preserve_existing_jieba_behavior(self, text):
        reference = jieba.Tokenizer()
        assert tokenize(text) == [word for word in reference.cut(text) if word.strip()]

    def test_fields_tokenized_separately_and_stopwords_only_filter_queries(self, tmp_path):
        path = tmp_path / "stopwords.txt"
        path.write_text("\ufeffAlpha\n的\n", encoding="utf-8")
        tokenizer = TextTokenizer(TokenizationConfig(stopwords_path=str(path)))
        assert tokenizer.query_terms("Alpha Beta 的 alpha") == ["Beta", "alpha"]
        assert tokenizer.token_text(["Alpha Beta", "的"]) == "Alpha Beta 的"
        assert token_text(["A", "I"]) == "A I"

    def test_custom_dictionary_used_for_index_and_query(self, tmp_path):
        path = tmp_path / "domain.txt"
        path.write_text("智识回溯协议 1000000 n\n", encoding="utf-8")
        tokenizer = TextTokenizer(TokenizationConfig(user_dictionaries=[str(path)]))
        assert tokenizer.query_terms("智识回溯协议") == ["智识回溯协议"]
        assert tokenizer.token_text(["智识回溯协议"]) == "智识回溯协议"


class TestAssetVersions:
    def test_content_versions_do_not_depend_on_file_location(self, tmp_path):
        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        data = "智识回溯协议 1000000 n\n".encode("utf-8")
        first.write_bytes(data)
        second.write_bytes(data)
        a = TextTokenizer(TokenizationConfig(user_dictionaries=[str(first)]))
        b = TextTokenizer(TokenizationConfig(user_dictionaries=[str(second)]))
        assert a.manifest == b.manifest
        assert a.manifest.asset_id == b.manifest.asset_id
        assert a.manifest.user_dictionary_sha256 == (hashlib.sha256(data).hexdigest(),)
        assert a.manifest.jieba_version == jieba.__version__
        assert len(a.manifest.base_dictionary_sha256) == 64

    def test_file_changes_do_not_mutate_existing_snapshot(self, tmp_path):
        dictionary = tmp_path / "domain.txt"
        stops = tmp_path / "stopwords.txt"
        dictionary.write_text("智识回溯协议 1000000 n\n", encoding="utf-8")
        stops.write_text("Alpha\n", encoding="utf-8")
        config = TokenizationConfig(user_dictionaries=[str(dictionary)], stopwords_path=str(stops))
        before = TextTokenizer(config)
        manifest = before.manifest
        dictionary.write_text("智识回溯协议新版 1000000 n\n", encoding="utf-8")
        stops.write_text("Beta\n", encoding="utf-8")
        after = TextTokenizer(config)
        assert before.manifest == manifest
        assert before.tokenize("智识回溯协议") == ["智识回溯协议"]
        assert before.query_terms("Alpha Beta") == ["Beta"]
        assert after.query_terms("Alpha Beta") == ["Alpha"]
        assert after.manifest.asset_id != manifest.asset_id

    def test_dictionary_order_is_part_of_asset_version(self, tmp_path):
        first, second = tmp_path / "one.txt", tmp_path / "two.txt"
        first.write_text("智识回溯协议 1000000\n", encoding="utf-8")
        second.write_text("智识回溯协议 1000\n", encoding="utf-8")
        a = TextTokenizer(TokenizationConfig(user_dictionaries=[str(first), str(second)]))
        b = TextTokenizer(TokenizationConfig(user_dictionaries=[str(second), str(first)]))
        assert a.manifest.asset_id != b.manifest.asset_id

    @pytest.mark.parametrize("data", [b'\xff', "智识回溯协议 0 n\n".encode(), "智识回溯协议 000\n".encode()])
    def test_invalid_dictionary_does_not_change_shared_tokenizer(self, tmp_path, data):
        before = tokenize("智识回溯协议")
        path = tmp_path / "invalid.txt"
        path.write_bytes(data)
        with pytest.raises(TokenizationError):
            TextTokenizer(TokenizationConfig(user_dictionaries=[str(path)]))
        assert tokenize("智识回溯协议") == before

    def test_missing_or_invalid_stopwords_fail_explicitly(self, tmp_path):
        path = tmp_path / "missing.txt"
        with pytest.raises(TokenizationError) as caught:
            TextTokenizer(TokenizationConfig(stopwords_path=str(path)))
        assert caught.value.code == "TOKENIZATION_FAILED"
        path.write_bytes(b'\xff')
        with pytest.raises(TokenizationError):
            TextTokenizer(TokenizationConfig(stopwords_path=str(path)))


class TestFtsAssetRebuild:
    def test_rebuild_uses_new_assets_and_preserves_facts(self, connection, tmp_path, monkeypatch):
        first = add_knowledge(connection, "智识回溯协议")
        inactive = add_knowledge(connection, "停用证据", status=1)
        categories = CategoryService(connection)
        category = categories.create("直接分类")
        categories.attach_knowledge(category.id, first)
        AliasService(connection).add(1, category.id, "分类别名", confirmed=True)
        AliasService(connection).add(0, first, "知识别名", confirmed=True)
        with connection:
            connection.execute("INSERT INTO knowledge_fts (rowid, content_tokens, knowledge_id, version_id) VALUES (?, '过期', ?, 999)", (inactive, inactive))
            connection.execute("INSERT INTO knowledge_fts (rowid, content_tokens, knowledge_id, version_id) VALUES (999, '孤立', 999, 999)")
        tables = ("knowledge", "knowledge_version", "alias", "category", "knowledge_category", "task_process")
        facts = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        dictionary = tmp_path / "domain.txt"
        dictionary.write_text("智识回溯协议 1000000 n\n", encoding="utf-8")
        config = Settings(tokenization={"user_dictionaries": [str(dictionary)]})
        monkeypatch.setattr(tokenization_module, "get_settings", lambda: config)
        reset_tokenizer_cache()
        try:
            assert rebuild_fts(connection) == 1
            assert connection.execute("SELECT rowid FROM knowledge_fts").fetchall() == [(first,)]
            assert connection.execute("SELECT knowledge_id FROM knowledge_fts WHERE knowledge_fts MATCH ?", ('"智识回溯协议"',)).fetchall() == [(first,)]
            assert "分类" in connection.execute("SELECT category_tokens FROM knowledge_fts").fetchone()[0]
            assert "知识" in connection.execute("SELECT alias_tokens FROM knowledge_fts").fetchone()[0]
            assert get_tokenizer().query_terms("智识回溯协议") == ["智识回溯协议"]
            assert {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == facts
            assert asdict(get_tokenizer().manifest)["format_version"] == 1
        finally:
            reset_tokenizer_cache()

    def test_rebuild_failure_restores_entire_old_index(self, connection):
        add_knowledge(connection, "保留旧索引")
        with connection:
            connection.execute("INSERT INTO knowledge DEFAULT VALUES")
        before = connection.execute("SELECT * FROM knowledge_fts ORDER BY rowid").fetchall()
        with pytest.raises(KnowledgeManagementError, match="当前版本缺失"):
            rebuild_fts(connection)
        assert connection.execute("SELECT * FROM knowledge_fts ORDER BY rowid").fetchall() == before

    def test_rebuild_joins_outer_transaction(self, connection):
        first = add_knowledge(connection, "正式内容")
        with connection:
            connection.execute("UPDATE knowledge_fts SET content_tokens = '旧索引' WHERE rowid = ?", (first,))
        old = connection.execute("SELECT * FROM knowledge_fts").fetchall()
        connection.execute("BEGIN IMMEDIATE")
        assert rebuild_fts(connection) == 1
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute("SELECT * FROM knowledge_fts").fetchall() == old
