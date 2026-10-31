"""V0.8 固定切片词元统计；与 FTS 用户词典和模型计费隔离。"""

from __future__ import annotations

import hashlib
import io
import json
from functools import lru_cache
from importlib.resources import files

import jieba

from intellect.core.errors import IntellectError

_EXPECTED = {
    "protocol": "jieba-nonspace-v1", "jieba_version": "0.42.1",
    "dictionary_sha256": "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8",
    "cut_all": False, "HMM": False, "exclude": "isspace",
}


class TokenCountError(IntellectError):
    code = "TOKEN_COUNT_FAILED"


class _FixedTokenizer(jieba.Tokenizer):
    def __init__(self, dictionary: bytes):
        """实现 `__init__` 生命周期钩子，初始化或维护对象状态。 参数包括：`dictionary`。"""
        super().__init__()
        self._dictionary = dictionary
        self.cache_file = "intellect-count-" + _EXPECTED["dictionary_sha256"] + ".cache"

    def get_dict_file(self):
        """执行 `get_dict_file` 对应的功能并返回处理结果。 参数包括：无显式参数。"""
        return io.BytesIO(self._dictionary)


class ChunkTokenCounter:
    def __init__(self):
        """实现 `__init__` 生命周期钩子，初始化或维护对象状态。 参数包括：无显式参数。"""
        try:
            resource = files("intellect.doc").joinpath("assets")
            metadata = json.loads(resource.joinpath("token_counter.json").read_text(encoding="utf-8"))
            dictionary = resource.joinpath("jieba-0.42.1.dict.txt").read_bytes()
            if (metadata != _EXPECTED or jieba.__version__ != metadata["jieba_version"]
                    or hashlib.sha256(dictionary).hexdigest() != metadata["dictionary_sha256"]):
                raise ValueError("统计器资源或版本不匹配")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TokenCountError("无法校验固定词元统计资源") from exc
        self._tokenizer = _FixedTokenizer(dictionary)

    @property
    def manifest(self) -> dict:
        """执行 `manifest` 对应的功能并返回处理结果。 参数包括：无显式参数。"""
        return dict(_EXPECTED)

    def count(self, text: str) -> int:
        """执行 `count` 对应的功能并返回处理结果。 参数包括：`text`。"""
        if not isinstance(text, str):
            raise TokenCountError("统计输入必须为文本")
        return sum(not token.isspace() for token in self._tokenizer.cut(text, cut_all=False, HMM=False))


@lru_cache(maxsize=1)
def get_counter() -> ChunkTokenCounter:
    """执行 `get_counter` 对应的功能并返回处理结果。 参数包括：无显式参数。"""
    return ChunkTokenCounter()
