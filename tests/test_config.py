"""T0.3 配置中心测试：默认值、校验规则、TOML 覆盖与环境变量注入。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from intellect.core.config import (
    ENV_EMBEDDING_API_KEY,
    ENV_LLM_API_KEY,
    ChunkConfig,
    DedupConfig,
    Settings,
    load_settings,
)


class TestDefaults:
    """默认值必须与 DESIGN.md 一致。"""

    def test_dedup_thresholds_match_design(self):
        settings = Settings()
        assert settings.dedup.auto_merge_threshold == 0.80
        assert settings.dedup.gray_zone_threshold == 0.60

    def test_fts_short_question_max_terms_default(self):
        assert Settings().fts.short_question_max_terms == 4


class TestValidation:
    def test_gray_threshold_must_be_below_auto(self):
        with pytest.raises(ValidationError):
            DedupConfig(auto_merge_threshold=0.80, gray_zone_threshold=0.80)

    def test_threshold_range(self):
        with pytest.raises(ValidationError):
            DedupConfig(auto_merge_threshold=1.5, gray_zone_threshold=0.6)

    def test_overlap_must_be_below_chunk_size(self):
        with pytest.raises(ValidationError):
            ChunkConfig(chunk_size=100, chunk_overlap=100)

    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError):
            Settings(**{"dedup": {"threshold": 0.9}})


class TestLoad:
    def test_load_without_file_uses_defaults(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INTELLECT_CONFIG", raising=False)
        settings = load_settings(tmp_path / "absent.toml")
        assert settings.dedup.auto_merge_threshold == 0.80

    def test_toml_overrides_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[dedup]\nauto_merge_threshold = 0.85\n[fts]\ntop_k = 5\n',
            encoding="utf-8",
        )
        settings = load_settings(config_file)
        assert settings.dedup.auto_merge_threshold == 0.85
        assert settings.dedup.gray_zone_threshold == 0.60  # 未覆盖项保持默认
        assert settings.fts.top_k == 5

    def test_invalid_toml_value_raises(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[dedup]\nauto_merge_threshold = 0.5\n", encoding="utf-8")
        with pytest.raises(ValidationError):
            load_settings(config_file)

    def test_api_key_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_LLM_API_KEY, "sk-test-llm")
        monkeypatch.setenv(ENV_EMBEDDING_API_KEY, "sk-test-emb")
        settings = load_settings(tmp_path / "absent.toml")
        assert settings.llm.api_key == "sk-test-llm"
        assert settings.embedding.api_key == "sk-test-emb"

    def test_config_path_from_env(self, tmp_path, monkeypatch):
        config_file = tmp_path / "custom.toml"
        config_file.write_text("[fts]\ntop_k = 3\n", encoding="utf-8")
        monkeypatch.setenv("INTELLECT_CONFIG", str(config_file))
        settings = load_settings()
        assert settings.fts.top_k == 3
