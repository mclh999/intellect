"""knowledge-v1：保留完整 token 数组的固定正文比较协议（DESIGN §5.2）。"""

from __future__ import annotations

import hashlib
import io
import json
from functools import lru_cache
from importlib.resources import files

import jieba

from intellect.core.errors import IntellectError

_PROTOCOL = {
    "algorithm": "sha256", "encoding": "utf-8", "errors": "strict",
    "normalization": "crlf-cr-to-lf", "prefix": "knowledge-v1:sha256:",
    "jieba_version": "0.42.1",
    "dictionary_sha256": "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8",
    "cut_all": False, "HMM": False, "include_whitespace": True, "include_punctuation": True,
    "serialization": {"format": "json-array", "ensure_ascii": False, "separators": [",", ":"]},
}


class KnowledgeHashError(IntellectError):
    code = "KNOWLEDGE_HASH_FAILED"


class _HashTokenizer(jieba.Tokenizer):
    def __init__(self, dictionary: bytes):
        """实现 `__init__` 生命周期钩子，初始化或维护对象状态。 参数包括：`dictionary`。"""
        super().__init__()
        self._snapshot = dictionary
        self.cache_file = "intellect-knowledge-v1-" + _PROTOCOL["dictionary_sha256"] + ".cache"

    def get_dict_file(self):
        """执行 `get_dict_file` 对应的功能并返回处理结果。 参数包括：无显式参数。"""
        return io.BytesIO(self._snapshot)


class KnowledgeHasher:
    """独立实例从随包快照加载，不使用 FTS 用户词典或停用词。"""

    def __init__(self):
        """实现 `__init__` 生命周期钩子，初始化或维护对象状态。 参数包括：无显式参数。"""
        try:
            resources = files("intellect.doc")
            manifest = json.loads(resources.joinpath("hash_protocols.json").read_text(encoding="utf-8"))
            dictionary = resources.joinpath("assets/jieba-0.42.1.dict.txt").read_bytes()
            if (manifest.get("knowledge-v1") != _PROTOCOL or jieba.__version__ != _PROTOCOL["jieba_version"]
                    or hashlib.sha256(dictionary).hexdigest() != _PROTOCOL["dictionary_sha256"]):
                raise ValueError("knowledge-v1 清单、分词版本或词典快照不匹配")
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise KnowledgeHashError("无法校验固定知识哈希协议与资源") from exc
        self._tokenizer = _HashTokenizer(dictionary)

    @property
    def manifest(self) -> dict:
        """执行 `manifest` 对应的功能并返回处理结果。 参数包括：无显式参数。"""
        return json.loads(json.dumps(_PROTOCOL))

    def hash(self, content: str) -> str:
        """执行 `hash` 对应的功能并返回处理结果。 参数包括：`content`。"""
        if not isinstance(content, str):
            raise KnowledgeHashError("知识哈希输入必须是完整正文字符串")
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        try:
            normalized.encode("utf-8", errors="strict")
            tokens = list(self._tokenizer.cut(normalized, cut_all=False, HMM=False))
            if "".join(tokens) != normalized:
                raise KnowledgeHashError("分词结果未保留完整正文")
            payload = json.dumps(tokens, ensure_ascii=False, separators=(",", ":")).encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise KnowledgeHashError("知识正文不能严格编码为 UTF-8") from exc
        return _PROTOCOL["prefix"] + hashlib.sha256(payload).hexdigest()


@lru_cache(maxsize=1)
def get_knowledge_hasher() -> KnowledgeHasher:
    """执行 `get_knowledge_hasher` 对应的功能并返回处理结果。 参数包括：无显式参数。"""
    return KnowledgeHasher()


def knowledge_hash(content: str) -> str:
    """候选比较及正式版本写入共用此入口；不包含标题，也不改写正文。"""
    return get_knowledge_hasher().hash(content)
