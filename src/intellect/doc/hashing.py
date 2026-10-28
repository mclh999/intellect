"""切片证据哈希；其他用途的业务哈希随对应导入/审核任务交付。"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.resources import files

from intellect.core.errors import IntellectError


class ChunkHashError(IntellectError):
    code = "CHUNK_HASH_FAILED"


@lru_cache(maxsize=1)
def _prefix() -> str:
    expected = {
        "algorithm": "sha256", "encoding": "utf-8", "errors": "strict",
        "normalization": "identity", "prefix": "chunk-v1:sha256:",
    }
    try:
        manifest = json.loads(files("intellect.doc").joinpath("hash_protocols.json").read_text(encoding="utf-8"))
        if manifest.get("chunk-v1") != expected:
            raise ValueError("chunk-v1 协议清单与实现不一致")
    except (OSError, ValueError, AttributeError) as exc:
        raise ChunkHashError("无法校验切片哈希协议清单") from exc
    return expected["prefix"]


def chunk_hash(content: str) -> str:
    """原文严格 UTF-8，不改变换行、空白、大小写或任何 Unicode 字符。"""
    if not isinstance(content, str):
        raise ChunkHashError("切片原文必须为字符串")
    try:
        payload = content.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ChunkHashError("切片原文不能严格编码为 UTF-8") from exc
    return _prefix() + hashlib.sha256(payload).hexdigest()
