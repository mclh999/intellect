"""T2.5：OR/转义、阈值与概念、排序、版本过滤及多查询读快照。"""

from __future__ import annotations

from contextlib import closing

import pytest

from intellect.core.config import FtsConfig, TokenizationConfig
from intellect.db import connect_database, initialize_database
from intellect.index.fts import rebuild_fts
from intellect.knowledge.fts import refresh_knowledge
from intellect.search.fts import FtsSearchError, FtsSearchService, or_query
from intellect.search.tokenization import TextTokenizer


@pytest.fixture
def database_path(tmp_path):
    """执行 `database_path` 对应的功能并返回处理结果。 参数包括：`tmp_path`。"""
    path = tmp_path / "search.db"
    initialize_database(path)
    return path


@pytest.fixture
def connection(database_path):
    """执行 `connection` 对应的功能并返回处理结果。 参数包括：`database_path`。"""
    with closing(connect_database(database_path)) as database:
        yield database


def add_knowledge(connection, content, title=None):
    """执行 `add_knowledge` 对应的功能并返回处理结果。 参数包括：`connection`、`content`、`title`。"""
    with connection:
        kid = connection.execute("INSERT INTO knowledge DEFAULT VALUES").lastrowid
        vid = connection.execute(
            "INSERT INTO knowledge_version (knowledge_id, version_no, title, content, content_hash, change_type) "
            "VALUES (?, 1, ?, ?, 'test-hash', 0)", (kid, title, content),
        ).lastrowid
        connection.execute("UPDATE knowledge SET current_version_id = ? WHERE id = ?", (vid, kid))
        refresh_knowledge(connection, kid)
    return kid, vid


class TestOrQueries:
    def test_or_recalls_documents_with_different_terms(self, connection):
        """验证 `or_recalls_documents_with_different_terms` 所代表的行为。 参数包括：`connection`。"""
        first, _ = add_knowledge(connection, "原子性")
        second, _ = add_knowledge(connection, "可见性")
        assert {hit.knowledge_id for hit in FtsSearchService(connection).search("原子性 可见性")} == {first, second}

    def test_terms_quoted_and_embedded_quotes_escaped(self, connection):
        """验证 `terms_quoted_and_embedded_quotes_escaped` 所代表的行为。 参数包括：`connection`。"""
        assert or_query(['alpha"beta', 'OR', 'C++', '*']) == '"alpha""beta" OR "OR" OR "C++" OR "*"'
        key, _ = add_knowledge(connection, 'Alpha OR Beta C++ quote"word')
        service = FtsSearchService(connection)
        for text in ('OR', 'C++', 'quote"word', '"Alpha"', 'Alpha*', '-Beta'):
            assert [hit.knowledge_id for hit in service.search(text)] == [key]
        assert service.search('"') == []

    def test_user_text_is_bound_and_cannot_change_sql(self, connection):
        """验证 `user_text_is_bound_and_cannot_change_sql` 所代表的行为。 参数包括：`connection`。"""
        kid, _ = add_knowledge(connection, "Alpha")
        service = FtsSearchService(connection, config=FtsConfig(short_question_max_terms=100))
        assert service.search("'; DROP TABLE knowledge; --") == []
        assert connection.execute("SELECT id FROM knowledge").fetchall() == [(kid,)]

    def test_empty_or_stopwords_only_question_does_not_query(self, connection, tmp_path):
        """验证 `empty_or_stopwords_only_question_does_not_query` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        path = tmp_path / "stopwords.txt"
        path.write_text("Alpha\nBeta\n", encoding="utf-8")
        tokenizer = TextTokenizer(TokenizationConfig(stopwords_path=str(path)))
        service = FtsSearchService(connection, tokenizer=tokenizer)
        statements = []
        connection.set_trace_callback(statements.append)
        assert service.search(" \n\t ") == []
        assert service.search("Alpha Beta") == []
        assert statements == []

    def test_custom_dictionary_shared_with_index(self, connection, tmp_path):
        """验证 `custom_dictionary_shared_with_index` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        kid, _ = add_knowledge(connection, "智识回溯协议")
        dictionary = tmp_path / "domain.txt"
        dictionary.write_text("智识回溯协议 1000000 n\n", encoding="utf-8")
        tokenizer = TextTokenizer(TokenizationConfig(user_dictionaries=[str(dictionary)]))
        rebuild_fts(connection, tokenizer=tokenizer)
        assert [hit.knowledge_id for hit in FtsSearchService(connection, tokenizer=tokenizer).search("智识回溯协议")] == [kid]


class TestConceptQueries:
    def test_configured_threshold_is_inclusive_and_counts_after_stopwords(self, connection, tmp_path):
        """验证 `configured_threshold_is_inclusive_and_counts_after_stopwords` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        path = tmp_path / "stopwords.txt"
        path.write_text("Stop\n", encoding="utf-8")
        tokenizer = TextTokenizer(TokenizationConfig(stopwords_path=str(path)))
        add_knowledge(connection, "Alpha Beta")
        calls = []

        def extract(question):
            """执行 `extract` 对应的功能并返回处理结果。 参数包括：`question`。"""
            calls.append(question)
            return ["Alpha", "Beta"]

        service = FtsSearchService(connection, config=FtsConfig(short_question_max_terms=2), tokenizer=tokenizer, concept_extractor=extract)
        assert service.search("Alpha Beta Stop")
        assert calls == []
        assert service.search("Alpha Beta Gamma Stop")
        assert calls == ["Alpha Beta Gamma Stop"]

    def test_long_question_queries_each_concept_then_deduplicates(self, connection):
        """验证 `long_question_queries_each_concept_then_deduplicates` 所代表的行为。 参数包括：`connection`。"""
        alpha, _ = add_knowledge(connection, "Alpha")
        beta, _ = add_knowledge(connection, "Beta")
        both, _ = add_knowledge(connection, "Alpha Beta")
        unrelated, _ = add_knowledge(connection, "Gamma")
        inputs = []
        service = FtsSearchService(connection, concept_extractor=lambda question: inputs.append(question) or ["Alpha", "Beta"])
        statements = []
        connection.set_trace_callback(statements.append)
        hits = service.search("Alpha Beta Gamma Delta Epsilon")
        assert inputs == ["Alpha Beta Gamma Delta Epsilon"]
        assert {hit.knowledge_id for hit in hits} == {alpha, beta, both}
        assert len(hits) == 3
        assert unrelated not in {hit.knowledge_id for hit in hits}
        assert len([statement for statement in statements if "WHERE knowledge_fts MATCH" in statement]) == 2

    def test_top_k_applies_per_concept_before_union(self, connection):
        """验证 `top_k_applies_per_concept_before_union` 所代表的行为。 参数包括：`connection`。"""
        alpha, _ = add_knowledge(connection, "Alpha")
        beta, _ = add_knowledge(connection, "Beta")
        service = FtsSearchService(connection, config=FtsConfig(top_k=1), concept_extractor=lambda _: ["Alpha", "Beta"])
        assert [hit.knowledge_id for hit in service.search("one two three four five")] == [alpha, beta]

    @pytest.mark.parametrize("concepts", [[], ["Alpha"], ["a", "b", "c", "d", "e"], "AB", ["Alpha", " "], ["Alpha", None]])
    def test_invalid_concepts_rejected(self, connection, concepts):
        """验证 `invalid_concepts_rejected` 所代表的行为。 参数包括：`connection`、`concepts`。"""
        service = FtsSearchService(connection, concept_extractor=lambda _: concepts)
        with pytest.raises(FtsSearchError, match="2–4"):
            service.search("one two three four five")
        assert not connection.in_transaction

    def test_missing_or_failed_extractor_never_falls_back_to_all_words(self, connection):
        """验证 `missing_or_failed_extractor_never_falls_back_to_all_words` 所代表的行为。 参数包括：`connection`。"""
        with pytest.raises(FtsSearchError, match="必须提供"):
            FtsSearchService(connection).search("one two three four five")

        def failure(question):
            """执行 `failure` 对应的功能并返回处理结果。 参数包括：`question`。"""
            raise TimeoutError("timeout")

        with pytest.raises(FtsSearchError, match="抽取失败") as caught:
            FtsSearchService(connection, concept_extractor=failure).search("one two three four five")
        assert isinstance(caught.value.__cause__, TimeoutError)


class TestRankAndVersions:
    def test_bm25_order_top_k_and_snapshot_fields(self, connection):
        """验证 `bm25_order_top_k_and_snapshot_fields` 所代表的行为。 参数包括：`connection`。"""
        add_knowledge(connection, "Alpha Alpha", title="Alpha")
        add_knowledge(connection, "Alpha Beta Gamma Delta Epsilon", title="第二条")
        add_knowledge(connection, "Alpha Zeta Eta Theta", title="第三条")
        hits = FtsSearchService(connection, config=FtsConfig(top_k=2)).search("Alpha")
        assert len(hits) == 2
        assert [hit.rank for hit in hits] == sorted(hit.rank for hit in hits)
        for hit in hits:
            assert connection.execute("SELECT title, content FROM knowledge_version WHERE id = ?", (hit.version_id,)).fetchone() == (hit.title, hit.content)

    def test_inactive_stale_foreign_version_and_wrong_rowid_excluded_before_limit(self, connection):
        """验证 `inactive_stale_foreign_version_and_wrong_rowid_excluded_before_limit` 所代表的行为。 参数包括：`connection`。"""
        inactive, _ = add_knowledge(connection, "Alpha")
        stale, stale_version = add_knowledge(connection, "Alpha")
        wrong_version, _ = add_knowledge(connection, "Alpha")
        valid, valid_version = add_knowledge(connection, "Alpha Beta")
        with connection:
            connection.execute("UPDATE knowledge SET status = 1 WHERE id = ?", (inactive,))
            latest = connection.execute("INSERT INTO knowledge_version (knowledge_id, version_no, content, content_hash, change_type) VALUES (?, 2, '新版本', 'new-hash', 2)", (stale,)).lastrowid
            connection.execute("UPDATE knowledge SET current_version_id = ? WHERE id = ?", (latest, stale))
            connection.execute("UPDATE knowledge SET current_version_id = ? WHERE id = ?", (stale_version, wrong_version))
            connection.execute("UPDATE knowledge_fts SET version_id = ? WHERE rowid = ?", (stale_version, wrong_version))
            connection.execute("INSERT INTO knowledge_fts (rowid, content_tokens, knowledge_id, version_id) VALUES (999, 'Alpha', ?, ?)", (valid, valid_version))
        hits = FtsSearchService(connection, config=FtsConfig(top_k=1)).search("Alpha")
        assert [hit.knowledge_id for hit in hits] == [valid]
        assert hits[0].version_id == valid_version

    def test_search_preserves_outer_transaction_and_never_writes(self, connection, database_path):
        """验证 `search_preserves_outer_transaction_and_never_writes` 所代表的行为。 参数包括：`connection`、`database_path`。"""
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO category (name) VALUES ('调用方未提交')")
        assert FtsSearchService(connection).search("Alpha") == []
        assert connection.in_transaction
        with closing(connect_database(database_path)) as observer:
            assert observer.execute("SELECT count(*) FROM category").fetchone()[0] == 0
        connection.rollback()

    def test_multiple_concepts_use_same_read_snapshot(self, connection, database_path):
        """验证 `multiple_concepts_use_same_read_snapshot` 所代表的行为。 参数包括：`connection`、`database_path`。"""
        first, _ = add_knowledge(connection, "Alpha")
        second, _ = add_knowledge(connection, "Beta")
        first_select = True

        def commit_concurrent_update(statement):
            """执行 `commit_concurrent_update` 对应的功能并返回处理结果。 参数包括：`statement`。"""
            nonlocal first_select
            if "WHERE knowledge_fts MATCH" in statement and not first_select:
                with closing(connect_database(database_path)) as writer, writer:
                    writer.execute("UPDATE knowledge SET status = 1 WHERE id = ?", (second,))
            if "WHERE knowledge_fts MATCH" in statement:
                first_select = False

        connection.set_trace_callback(commit_concurrent_update)
        service = FtsSearchService(connection, concept_extractor=lambda _: ["Alpha", "Beta"])
        assert [hit.knowledge_id for hit in service.search("one two three four five")] == [first, second]
        connection.set_trace_callback(None)
        assert FtsSearchService(connection).search("Beta") == []
