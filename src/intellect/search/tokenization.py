"""写入/查询共用的 jieba 资产快照（T2.3，DESIGN §7.3）。"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

import jieba

from intellect.core.config import TokenizationConfig, get_settings
from intellect.core.errors import IntellectError


class TokenizationError(IntellectError):
    code = "TOKENIZATION_FAILED"


@dataclass(frozen=True)
class TokenizationManifest:
    format_version: int
    jieba_version: str
    base_dictionary_sha256: str
    user_dictionary_sha256: tuple[str, ...]
    stopwords_sha256: str

    @property
    def asset_id(self) -> str:
        data = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(data.encode("utf-8")).hexdigest()


class _SnapshotTokenizer(jieba.Tokenizer):
    def __init__(self, dictionary: bytes):
        super().__init__()
        self._dictionary_snapshot = dictionary
        self.cache_file = "intellect-jieba-" + hashlib.sha256(dictionary).hexdigest() + ".cache"

    def get_dict_file(self):
        return io.BytesIO(self._dictionary_snapshot)


def _read_asset(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise TokenizationError("无法读取分词资产", context={"path": path}) from exc


class TextTokenizer:
    """构造后保持词典与停用词快照；外部文件变动不会悄悄改变在用实例。"""

    def __init__(self, config: TokenizationConfig | None = None):
        config = config if config is not None else get_settings().tokenization
        with jieba.Tokenizer().get_dict_file() as dictionary_file:
            dictionary = dictionary_file.read()
        user_dictionaries = [_read_asset(path) for path in config.user_dictionaries]
        stopwords = (_read_asset(config.stopwords_path) if config.stopwords_path is not None
                     else files("intellect.search").joinpath("assets/stopwords.txt").read_bytes())
        try:
            self._stopwords = frozenset(
                word.strip() for word in stopwords.decode("utf-8-sig").splitlines() if word.strip()
            )
            dictionaries = []
            for data in user_dictionaries:
                text = data.decode("utf-8-sig")
                for line in text.splitlines():
                    if not line.strip():
                        continue
                    match = re.fullmatch(r"(.+?)(?: ([0-9]+))?(?: ([a-z]+))?", line.strip())
                    if match is None or (match[2] is not None and int(match[2]) == 0):
                        # 零频词会修改 jieba 的进程全局 HMM 状态，破坏实例隔离。
                        raise ValueError("自定义词典频次须为正整数或省略")
                dictionaries.append(text)
            self._engine = _SnapshotTokenizer(dictionary)
            self._engine.initialize()
            for text in dictionaries:
                self._engine.load_userdict(io.StringIO(text))
        except (UnicodeError, ValueError) as exc:
            raise TokenizationError("分词资产格式非法") from exc
        self._manifest = TokenizationManifest(
            format_version=1,
            jieba_version=jieba.__version__,
            base_dictionary_sha256=hashlib.sha256(dictionary).hexdigest(),
            user_dictionary_sha256=tuple(hashlib.sha256(data).hexdigest() for data in user_dictionaries),
            stopwords_sha256=hashlib.sha256(stopwords).hexdigest(),
        )

    @property
    def manifest(self) -> TokenizationManifest:
        return self._manifest

    def tokenize(self, text: str) -> list[str]:
        return [word for word in self._engine.cut(text) if word.strip()]

    def query_terms(self, text: str) -> list[str]:
        return [word for word in self.tokenize(text) if word not in self._stopwords]

    def token_text(self, texts: Iterable[str]) -> str:
        return " ".join(word for text in texts for word in self.tokenize(text))


@lru_cache(maxsize=1)
def get_tokenizer() -> TextTokenizer:
    return TextTokenizer()


def reset_tokenizer_cache() -> None:
    """仅清缓存；资产升级须在维护窗口完成整体重建后恢复读写。"""
    get_tokenizer.cache_clear()


def tokenize(text: str) -> list[str]:
    return get_tokenizer().tokenize(text)


def token_text(texts: Iterable[str]) -> str:
    return get_tokenizer().token_text(texts)
