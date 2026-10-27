"""词典升级时从正式知识整体重建 FTS，不改写事实或向量同步状态。"""

from __future__ import annotations

import logging
import sqlite3

from intellect.knowledge._common import atomic
from intellect.knowledge.fts import refresh_knowledge
from intellect.search.tokenization import TextTokenizer, get_tokenizer

logger = logging.getLogger(__name__)


def rebuild_fts(connection: sqlite3.Connection, *, tokenizer: TextTokenizer | None = None) -> int:
    """维护窗口显式调用；参与外层事务，失败保留原 FTS，返回重建知识数。"""
    tokenizer = tokenizer if tokenizer is not None else get_tokenizer()
    with atomic(connection):
        connection.execute("DELETE FROM knowledge_fts")
        knowledge_ids = connection.execute("SELECT id FROM knowledge WHERE status = 0 ORDER BY id").fetchall()
        for (knowledge_id,) in knowledge_ids:
            refresh_knowledge(connection, knowledge_id, tokenizer=tokenizer)
    logger.info("FTS 整体重建已执行：%d 条，分词资产 %s", len(knowledge_ids), tokenizer.manifest.asset_id)
    return len(knowledge_ids)
