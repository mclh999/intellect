"""正式元数据的事务内 FTS 维护，不连接外部向量索引。"""

from __future__ import annotations

import sqlite3

from intellect.knowledge._common import KnowledgeManagementError
from intellect.search.tokenization import TextTokenizer, get_tokenizer


def refresh_knowledge(
    connection: sqlite3.Connection, knowledge_id: int, *, tokenizer: TextTokenizer | None = None,
) -> None:
    """调用方持有写事务；一知识一行，从当前版本与正式元数据重新生成。"""
    if not connection.in_transaction:
        raise KnowledgeManagementError("FTS 维护必须加入正式变更事务")
    row = connection.execute(
        "SELECT k.status, k.current_version_id, v.knowledge_id, v.title, v.content "
        "FROM knowledge k LEFT JOIN knowledge_version v ON v.id = k.current_version_id "
        "WHERE k.id = ?", (knowledge_id,),
    ).fetchone()
    if row is not None and row[0] == 0 and (row[1] is None or row[2] != knowledge_id):
        raise KnowledgeManagementError("有效知识的当前版本缺失或不属于该知识", context={
            "knowledge_id": knowledge_id, "version_id": row[1],
        })
    connection.execute("DELETE FROM knowledge_fts WHERE rowid = ?", (knowledge_id,))
    if row is None or row[0] != 0:
        return
    tokenizer = tokenizer if tokenizer is not None else get_tokenizer()
    aliases = [value[0] for value in connection.execute(
        "SELECT name FROM alias WHERE owner_type = 0 AND owner_id = ? ORDER BY id", (knowledge_id,)
    )]
    category_texts = []
    for category_id, name in connection.execute(
        "SELECT c.id, c.name FROM category c JOIN knowledge_category kc ON kc.category_id = c.id "
        "WHERE kc.knowledge_id = ? AND c.status = 0 ORDER BY c.id", (knowledge_id,)
    ).fetchall():
        category_texts.append(name)
        category_texts.extend(value[0] for value in connection.execute(
            "SELECT name FROM alias WHERE owner_type = 1 AND owner_id = ? ORDER BY id", (category_id,)
        ))
    connection.execute(
        "INSERT INTO knowledge_fts (rowid, title_tokens, content_tokens, alias_tokens, "
        "category_tokens, knowledge_id, version_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (knowledge_id, tokenizer.token_text([row[3] or ""]), tokenizer.token_text([row[4]]),
         tokenizer.token_text(aliases), tokenizer.token_text(category_texts), knowledge_id, row[1]),
    )


def refresh_owner(connection: sqlite3.Connection, owner_type: int, owner_id: int) -> None:
    if owner_type == 0:
        refresh_knowledge(connection, owner_id)
    else:
        knowledge_ids = connection.execute(
            "SELECT knowledge_id FROM knowledge_category WHERE category_id = ? ORDER BY knowledge_id",
            (owner_id,),
        ).fetchall()
        for (knowledge_id,) in knowledge_ids:
            refresh_knowledge(connection, knowledge_id)
