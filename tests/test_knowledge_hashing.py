"""knowledge-v1 固定输入/摘要、文本边界与专用资产校验。"""

from __future__ import annotations

import json
from importlib.resources import files
from types import SimpleNamespace

import pytest

from intellect.core.config import TokenizationConfig
from intellect.knowledge import hashing
from intellect.knowledge.hashing import KnowledgeHasher, KnowledgeHashError, knowledge_hash
from intellect.search.tokenization import TextTokenizer


class TestKnowledgeHashVectors:
    @pytest.mark.parametrize("text,digest", [
        ("", "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
        ("abc", "02f393ea9358560882c1fe797bf99d600aa4643a68276d8e3d714d1c4f19aecc"),
        ("a b", "5c1e46d364edd1b0efc07e4ba5b0e558b5e628457e0025164662feb400e28ab1"),
        ("A,B", "7c14df595d44601fff84d4ac1f4b1fa4146a9c87ebfc233e5543a05f46b38fc7"),
    ])
    def test_known_token_array_digests(self, text, digest):
        """验证 `known_token_array_digests` 所代表的行为。 参数包括：`text`、`digest`。"""
        assert knowledge_hash(text) == "knowledge-v1:sha256:" + digest

    def test_only_line_endings_normalized(self):
        """验证 `only_line_endings_normalized` 所代表的行为。 参数包括：无显式参数。"""
        assert knowledge_hash("条件\r\n结论\r例外") == knowledge_hash("条件\n结论\n例外")

    @pytest.mark.parametrize("left,right", [
        ("可以", "不可以"), ("10 mg", "100 mg"), ("10 mg", "10 g"),
        ("x>0", "x>=0"), ("A", "a"), ("a b", "ab"), ("a b", "a  b"),
        (" a", "a"), ("a\n", "a"), ("é", "e\u0301"), ("ＡＩ", "AI"),
        ("a,b", "a b"), ("\ufeffa", "a"), ("a\tb", "a b"),
    ])
    def test_meaning_whitespace_and_unicode_boundaries_retained(self, left, right):
        """验证 `meaning_whitespace_and_unicode_boundaries_retained` 所代表的行为。 参数包括：`left`、`right`。"""
        assert knowledge_hash(left) != knowledge_hash(right)

    @pytest.mark.parametrize("content", [b"abc", None, 1, "\ud800"])
    def test_invalid_text_rejected(self, content):
        """验证 `invalid_text_rejected` 所代表的行为。 参数包括：`content`。"""
        with pytest.raises(KnowledgeHashError):
            knowledge_hash(content)


class TestKnowledgeHashAssets:
    def test_fts_dictionary_and_stopwords_cannot_change_business_hash(self, tmp_path):
        """验证 `fts_dictionary_and_stopwords_cannot_change_business_hash` 所代表的行为。 参数包括：`tmp_path`。"""
        content = "智识回溯协议 不可以 10 mg"
        before = knowledge_hash(content)
        dictionary = tmp_path / "custom.txt"
        stops = tmp_path / "stops.txt"
        dictionary.write_text("智识回溯协议 1000000 n\n", encoding="utf-8")
        stops.write_text("不\n可以\n10\nmg\n", encoding="utf-8")
        fts = TextTokenizer(TokenizationConfig(user_dictionaries=[str(dictionary)], stopwords_path=str(stops)))
        assert "智识回溯协议" in fts.query_terms(content)
        assert knowledge_hash(content) == before

    def test_manifest_copies_do_not_mutate_protocol(self):
        """验证 `manifest_copies_do_not_mutate_protocol` 所代表的行为。 参数包括：无显式参数。"""
        hasher = KnowledgeHasher()
        manifest = hasher.manifest
        manifest["serialization"]["ensure_ascii"] = True
        assert hasher.manifest["serialization"]["ensure_ascii"] is False

    @pytest.mark.parametrize("target", ["manifest", "dictionary", "runtime"])
    def test_missing_or_mismatched_assets_fail_closed(self, tmp_path, monkeypatch, target):
        """验证 `missing_or_mismatched_assets_fail_closed` 所代表的行为。 参数包括：`tmp_path`、`monkeypatch`、`target`。"""
        resources = files("intellect.doc")
        manifest = json.loads(resources.joinpath("hash_protocols.json").read_text(encoding="utf-8"))
        manifest_path = tmp_path / "protocol.json"
        dictionary_path = resources.joinpath("assets/jieba-0.42.1.dict.txt")
        if target == "manifest":
            manifest["knowledge-v1"]["prefix"] = "knowledge-v2:sha256:"
        elif target == "dictionary":
            dictionary_path = tmp_path / "broken-dictionary.txt"
            dictionary_path.write_bytes(b"changed")
        else:
            monkeypatch.setattr(hashing.jieba, "__version__", "changed")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setattr(hashing, "files", lambda package: SimpleNamespace(
            joinpath=lambda name: manifest_path if name == "hash_protocols.json" else dictionary_path
        ))
        with pytest.raises(KnowledgeHashError) as caught:
            KnowledgeHasher()
        assert caught.value.code == "KNOWLEDGE_HASH_FAILED"

    def test_tokenizer_dropping_evidence_is_rejected(self):
        """验证 `tokenizer_dropping_evidence_is_rejected` 所代表的行为。 参数包括：无显式参数。"""
        hasher = KnowledgeHasher()
        hasher._tokenizer = SimpleNamespace(cut=lambda *args, **kwargs: ["abc"])
        with pytest.raises(KnowledgeHashError, match="完整正文"):
            hasher.hash(" abc ")
