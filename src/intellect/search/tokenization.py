"""FTS 共用的最小 jieba 文本分词入口（T1.5 写入所需）。

使用 uv.lock 固定的 jieba 及其内置词典；不独立升级词典或裁剪正文。
停用词资产版本化与查询构造在 T2.3/T2.5 单独交付。
"""

from __future__ import annotations

from collections.abc import Iterable

import jieba

_tokenizer = jieba.Tokenizer()


def tokenize(text: str) -> list[str]:
    return [word for word in _tokenizer.cut(text) if word.strip()]


def token_text(texts: Iterable[str]) -> str:
    """逐字段分词后空格拼接，字段边界不会被合成新词。"""
    return " ".join(word for text in texts for word in tokenize(text))
