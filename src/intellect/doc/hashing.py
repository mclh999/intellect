"""file/document/chunk-v1 固定协议；知识哈希随抽取/审核任务交付。"""

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


class DocumentHashError(IntellectError):
    code = "DOCUMENT_HASH_FAILED"


@lru_cache(maxsize=2)
def _document_prefix(protocol: str) -> str:
    expected = {"algorithm": "sha256", "prefix": f"{protocol}:sha256:"}
    if protocol == "file-v1":
        expected["input"] = "original-bytes"
    elif protocol == "document-v1":
        expected.update(encoding="utf-8", errors="strict", normalization="crlf-cr-to-lf")
    else:
        raise DocumentHashError("未知文档哈希协议")
    try:
        manifest = json.loads(files("intellect.doc").joinpath("hash_protocols.json").read_text(encoding="utf-8"))
        if manifest.get(protocol) != expected:
            raise ValueError("协议清单与实现不一致")
    except (OSError, ValueError, AttributeError) as exc:
        raise DocumentHashError("无法校验文档哈希协议清单") from exc
    return expected["prefix"]


def file_hash(content: bytes) -> str:
    if not isinstance(content, bytes):
        raise DocumentHashError("原文件哈希必须使用原始字节")
    return _document_prefix("file-v1") + hashlib.sha256(content).hexdigest()


def document_hash(text: str) -> str:
    if not isinstance(text, str):
        raise DocumentHashError("文档正文必须为字符串")
    try:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise DocumentHashError("文档正文不能严格编码为 UTF-8") from exc
    return _document_prefix("document-v1") + hashlib.sha256(normalized).hexdigest()
