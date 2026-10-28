"""T0.3 配置中心测试：默认值、校验规则、TOML 覆盖与环境变量注入。"""

from __future__ import annotations

import tomllib

import pytest
from pydantic import ValidationError

from intellect.core.config import (
    ENV_EMBEDDING_API_KEY,
    ENV_LLM_API_KEY,
    ChunkConfig,
    DedupConfig,
    DocumentConfig,
    Settings,
    TokenizationConfig,
    load_settings,
)


class TestDefaults:
    def test_document_engineering_defaults(self):
        assert Settings().document.text_encoding == "utf-8"
        assert Settings().document.web_timeout_seconds == 30.0

    def test_tokenization_uses_builtin_assets_by_default(self):
        assert Settings().tokenization == TokenizationConfig()
        assert Settings().tokenization.user_dictionaries == []
        assert Settings().tokenization.stopwords_path is None

    """默认值必须与 DESIGN.md 一致。"""

    def test_dedup_thresholds_match_design(self):
        settings = Settings()
        assert settings.dedup.auto_merge_threshold == 0.80
        assert settings.dedup.gray_zone_threshold == 0.60

    def test_fts_short_question_max_terms_default(self):
        assert Settings().fts.short_question_max_terms == 4


class TestValidation:
    @pytest.mark.parametrize("values", [{"text_encoding":""}, {"text_encoding":"no-such-codec"},
        {"text_encoding":"base64_codec"}, {"web_timeout_seconds":0}, {"web_timeout_seconds":float('inf')},
        {"web_timeout_seconds":float('nan')}, {"unknown":1}])
    def test_invalid_document_configuration_rejected(self, values):
        with pytest.raises(ValidationError):
            DocumentConfig(**values)

    @pytest.mark.parametrize("values", [
        {"user_dictionaries": "dictionary.txt"},
        {"user_dictionaries": [""]},
        {"stopwords_path": ""},
        {"unexpected": True},
    ])
    def test_tokenization_asset_configuration_rejected_when_invalid(self, values):
        with pytest.raises(ValidationError):
            TokenizationConfig(**values)

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
    def test_document_configuration_from_toml(self, tmp_path):
        path = tmp_path / "document.toml"
        path.write_text('[document]\ntext_encoding="gb18030"\nweb_timeout_seconds=9.0\n',encoding="utf-8")
        assert load_settings(path).document == DocumentConfig(text_encoding='gb18030',web_timeout_seconds=9)

    def test_tokenization_configuration_from_toml(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[tokenization]\nuser_dictionaries = ["config/domain.txt"]\n'
                        'stopwords_path = "config/stopwords.txt"\n', encoding="utf-8")
        assert load_settings(path).tokenization == TokenizationConfig(
            user_dictionaries=["config/domain.txt"], stopwords_path="config/stopwords.txt"
        )

    @pytest.mark.parametrize("section", ["llm", "embedding"])
    @pytest.mark.parametrize("env_enabled", [False, True])
    def test_api_key_in_file_rejected(self, tmp_path, monkeypatch, section, env_enabled):
        for variable in (ENV_LLM_API_KEY, ENV_EMBEDDING_API_KEY):
            monkeypatch.delenv(variable, raising=False)
            if env_enabled:
                monkeypatch.setenv(variable, "test-env-key")
        config_file = tmp_path / "config.toml"
        config_file.write_text(f'[{section}]\napi_key = "test-file-key"\n', encoding="utf-8")
        with pytest.raises(ValueError, match="api_key") as caught:
            load_settings(config_file)
        assert "test-file-key" not in str(caught.value)

    def test_api_keys_hidden_in_repr_and_dump(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_LLM_API_KEY, "test-llm-secret")
        monkeypatch.setenv(ENV_EMBEDDING_API_KEY, "test-embedding-secret")
        settings = load_settings(tmp_path / "absent.toml")
        assert "test-llm-secret" not in repr(settings)
        assert "test-embedding-secret" not in str(settings)
        assert "api_key" not in settings.model_dump()["llm"]
        assert "api_key" not in settings.model_dump()["embedding"]

    def test_config_directory_is_not_missing_file(self, tmp_path):
        with pytest.raises(OSError):
            load_settings(tmp_path)

    def test_malformed_toml_rejected(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[dedup", encoding="utf-8")
        with pytest.raises(tomllib.TOMLDecodeError):
            load_settings(config_file)

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
