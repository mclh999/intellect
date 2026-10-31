"""FTS5 关键词召回（T2.5）：安全 OR、长问题分概念与当前版本校验。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from intellect.core.config import FtsConfig, get_settings
from intellect.core.errors import IntellectError
from intellect.search.tokenization import TextTokenizer, get_tokenizer


class FtsSearchError(IntellectError):
    code = "FTS_SEARCH_FAILED"


@dataclass(frozen=True)
class FtsHit:
    knowledge_id: int
    version_id: int
    title: str | None
    content: str
    rank: float


def or_query(terms: Sequence[str]) -> str:
    """每词双引号包裹，内部引号双写；输出作为 MATCH 参数绑定。"""
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


@contextmanager
def _read_snapshot(connection: sqlite3.Connection):
    # SAVEPOINT 在独立查询时打开读事务，在调用方事务内则保留外层所有权。
    """执行 `read_snapshot` 所属的内部处理步骤。 参数包括：`connection`。"""
    name = "fts_read_" + uuid4().hex
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
        connection.execute(f"RELEASE SAVEPOINT {name}")
    except BaseException:
        connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
        connection.execute(f"RELEASE SAVEPOINT {name}")
        raise


class FtsSearchService:
    """FTS 召回原语；问答意图理解和模型装配分别属于 T4.1/T4.3。"""

    def __init__(
        self, connection: sqlite3.Connection, *, config: FtsConfig | None = None,
        tokenizer: TextTokenizer | None = None,
        concept_extractor: Callable[[str], Sequence[str]] | None = None,
    ):
        """实现 `__init__` 生命周期钩子，初始化或维护对象状态。 参数包括：`connection`、`config`、`tokenizer`、`concept_extractor`。"""
        self.connection = connection
        self.config = (config if config is not None else get_settings().fts).model_copy(deep=True)
        self.tokenizer = tokenizer if tokenizer is not None else get_tokenizer()
        self.concept_extractor = concept_extractor

    def _queries(self, question: str) -> list[str]:
        """执行 `queries` 所属的内部处理步骤。 参数包括：`question`。"""
        if not isinstance(question, str):
            raise FtsSearchError("问题必须为文本")
        terms = self.tokenizer.query_terms(question)
        if not terms:
            return []
        if len(terms) <= self.config.short_question_max_terms:
            return [or_query(terms)]
        if self.concept_extractor is None:
            raise FtsSearchError("长问题必须提供关键概念抽取器")
        try:
            concepts = self.concept_extractor(question)
        except Exception as exc:
            raise FtsSearchError("关键概念抽取失败") from exc
        if (not isinstance(concepts, Sequence) or isinstance(concepts, (str, bytes))
                or not 2 <= len(concepts) <= 4
                or any(not isinstance(concept, str) or not concept.strip() for concept in concepts)):
            raise FtsSearchError("关键概念抽取器必须返回 2–4 个非空文本概念")
        return [query for concept in concepts if (query := or_query(self.tokenizer.query_terms(concept)))]

    def search(self, question: str) -> list[FtsHit]:
        # 模型/抽取器调用在读事务之前完成，避免在模型等待期间持有数据库快照。
        """执行 `search` 对应的功能并返回处理结果。 参数包括：`question`。"""
        queries = self._queries(question)
        if not queries:
            return []
        hits: dict[int, FtsHit] = {}
        try:
            with _read_snapshot(self.connection):
                for query in queries:
                    rows = self.connection.execute(
                        "SELECT knowledge_fts.knowledge_id, knowledge_fts.version_id, v.title, v.content, "
                        "knowledge_fts.rank FROM knowledge_fts "
                        "JOIN knowledge k ON k.id = knowledge_fts.knowledge_id "
                        "AND k.id = knowledge_fts.rowid AND k.status = 0 "
                        "AND k.current_version_id = knowledge_fts.version_id "
                        "JOIN knowledge_version v ON v.id = knowledge_fts.version_id AND v.knowledge_id = k.id "
                        "WHERE knowledge_fts MATCH ? ORDER BY knowledge_fts.rank, knowledge_fts.rowid LIMIT ?",
                        (query, self.config.top_k),
                    ).fetchall()
                    for row in rows:
                        hit = FtsHit(*row)
                        hits.setdefault(hit.knowledge_id, hit)
        except sqlite3.Error as exc:
            raise FtsSearchError("FTS 查询失败") from exc
        # 每概念按 rank 取 top-K 后按召回顺序合并；不跨概念相加或重排分数。
        return list(hits.values())
