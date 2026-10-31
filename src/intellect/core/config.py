"""配置中心（T0.3）：全部可调参数集中于此，禁止散落硬编码。

读取顺序（后者覆盖前者）：
1. 内置默认值 —— DESIGN.md V0.4 规定去重阈值 0.80/0.60、短问题词数阈值 4；
   其他值为工程默认值，不代表新增业务规则；
2. TOML 配置文件 —— 默认 ``config/config.toml``（不入库），路径可用环境变量
   ``INTELLECT_CONFIG`` 指定；只提交注释完备的 ``config/config.example.toml``；
3. 环境变量 —— API Key 等敏感信息只经环境变量注入，禁止写入配置文件：
   ``INTELLECT_LLM_API_KEY``、``INTELLECT_EMBEDDING_API_KEY``。

本阶段暂不接数据库；后续如迁移到配置表，本模块仍是唯一读取入口。
"""

from __future__ import annotations

import codecs
import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 环境变量名约定
ENV_CONFIG_PATH = "INTELLECT_CONFIG"
ENV_LLM_API_KEY = "INTELLECT_LLM_API_KEY"
ENV_EMBEDDING_API_KEY = "INTELLECT_EMBEDDING_API_KEY"

DEFAULT_CONFIG_PATH = Path("config/config.toml")


class StrictModel(BaseModel):
    """禁止未知字段：配置文件写错键名时立即暴露，而非静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class DedupConfig(StrictModel):
    """知识级去重相似度阈值（DESIGN §5.2，判定序见设计稿）。

    s 为候选与已有有效知识当前版本正文的向量余弦相似度。
    """

    # s ≥ 阈值且最高分唯一、逐项一致性核对通过并满足验收门槛，才可自动合并来源
    auto_merge_threshold: float = Field(default=0.80, gt=0, lt=1)
    # gray_zone_threshold ≤ s < auto_merge_threshold → 灰区转人工；s < 该值 → 自动创建新知识
    gray_zone_threshold: float = Field(default=0.60, gt=0, lt=1)

    @model_validator(mode="after")
    def _check_ordering(self) -> "DedupConfig":
        """执行 `check_ordering` 所属的内部处理步骤。 参数包括：无显式参数。"""
        if self.gray_zone_threshold >= self.auto_merge_threshold:
            raise ValueError("gray_zone_threshold 必须小于 auto_merge_threshold")
        return self


class ChunkConfig(StrictModel):
    """原文切分参数（DESIGN §3.2 parse_config：块大小、重叠、分隔符）。"""

    chunk_size: int = Field(default=800, description="切片目标长度（字符）", gt=0)
    chunk_overlap: int = Field(default=100, description="相邻切片重叠（字符）", ge=0)
    separators: list[str] = Field(
        default=["\n\n", "\n", "。", "；", "，", " ", ""],
        description="切分分隔符，按优先级降序",
    )

    @model_validator(mode="after")
    def _check_overlap(self) -> "ChunkConfig":
        """执行 `check_overlap` 所属的内部处理步骤。 参数包括：无显式参数。"""
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        return self


class DocumentConfig(StrictModel):
    """解析工程参数；创建任务时冻结编码，网络仅用于显式 WEB 归档。"""

    text_encoding: str = Field(default="utf-8", min_length=1)
    web_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)

    @field_validator("text_encoding")
    @classmethod
    def _text_encoding(cls, value: str) -> str:
        """执行 `text_encoding` 所属的内部处理步骤。 参数包括：`value`。"""
        try:
            codecs.lookup(value)
            "a".encode(value)
        except (LookupError, ValueError) as exc:
            raise ValueError("text_encoding 必须是受支持的文本编码") from exc
        return value


class TokenizationConfig(StrictModel):
    """DESIGN §7.3：同一组词典与停用词供写入/查询共用，资产变化后整体重建 FTS。"""

    user_dictionaries: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    stopwords_path: str | None = Field(default=None, min_length=1)


class FtsConfig(StrictModel):
    """FTS5 关键词检索参数（DESIGN §7.3）。"""

    # 分词去停用词后词数 ≤ 该值，全部词直接进 OR 检索式；超过则先抽关键概念（2~4 个）
    short_question_max_terms: int = Field(default=4, ge=1)
    top_k: int = Field(default=20, ge=1, description="bm25(rank) 排序取 top-K")


class VectorConfig(StrictModel):
    """向量检索参数（DESIGN §7.4）。"""

    top_k: int = Field(default=20, ge=1)
    score_threshold: float = Field(default=0.0, ge=0, le=1, description="召回分数下限，0 为不过滤")


class LlmConfig(StrictModel):
    """抽取/问答 LLM 参数（DESIGN 2.1；T2.4 接入，model/base_url 使用前必须配置）。"""

    provider: str = Field(default="openai", description="OpenAI 协议兼容的推理服务")
    model: str = Field(default="", description="模型名，接入前必须配置")
    base_url: str = Field(default="", description="API 地址，接入前必须配置")
    api_key: str | None = Field(default=None, exclude=True, repr=False, description="仅经环境变量注入")
    temperature: float = Field(default=0.1, ge=0, le=2)


class EmbeddingConfig(StrictModel):
    """Embedding 模型参数。

    注意：模型注册与启用状态以数据库 embedding_model 表为准（DESIGN §3.4，
    全局至多一条 is_active=1）；此处仅存连接参数与默认维度，dimension 需与
    登记到 embedding_model 表的维度一致。
    """

    model_name: str = Field(default="bge-m3")
    dimension: int = Field(default=1024, gt=0)
    base_url: str = Field(default="", description="API 地址，使用本地模型时可留空")
    api_key: str | None = Field(default=None, exclude=True, repr=False, description="仅经环境变量注入")


class DatabaseConfig(StrictModel):
    """SQLite 连接参数（驱动为标准库 sqlite3；PRAGMA 为硬约束，不做成配置）。"""

    path: str = Field(default="data/intellect.db", description="数据库文件路径")


class LoggingConfig(StrictModel):
    """统一日志参数（格式见 core/logging.py，全系统唯一）。"""

    level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    dir: str = Field(default="logs", description="日志目录（不入库）")


class AuthConfig(StrictModel):
    """当前进程登录会话的有效期。"""

    session_ttl_seconds: int = Field(default=3600, gt=0, strict=True)


class Settings(StrictModel):
    """全局配置根模型。"""

    dedup: DedupConfig = DedupConfig()
    chunk: ChunkConfig = ChunkConfig()
    document: DocumentConfig = DocumentConfig()
    tokenization: TokenizationConfig = TokenizationConfig()
    fts: FtsConfig = FtsConfig()
    vector: VectorConfig = VectorConfig()
    llm: LlmConfig = LlmConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()
    auth: AuthConfig = AuthConfig()


def load_settings(config_path: str | Path | None = None) -> Settings:
    """加载配置：内置默认值 + TOML 覆盖 + 环境变量注入 API Key。

    配置文件不存在时直接使用默认值；文件存在但内容非法时抛出
    pydantic.ValidationError / tomllib.TOMLDecodeError，禁止带病启动。
    """
    path = Path(config_path or os.environ.get(ENV_CONFIG_PATH, DEFAULT_CONFIG_PATH))
    overrides: dict = {}
    try:
        with path.open("rb") as fh:
            overrides = tomllib.load(fh)
    except FileNotFoundError:
        pass

    for section in ("llm", "embedding"):
        values = overrides.get(section)
        if isinstance(values, dict) and "api_key" in values:
            raise ValueError(f"{section}.api_key 禁止写入配置文件，请通过环境变量注入")

    settings = Settings(**overrides)

    env_llm_key = os.environ.get(ENV_LLM_API_KEY)
    if env_llm_key:
        settings.llm.api_key = env_llm_key
    env_embedding_key = os.environ.get(ENV_EMBEDDING_API_KEY)
    if env_embedding_key:
        settings.embedding.api_key = env_embedding_key
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内缓存的配置单例；测试或热更新后用 reset_settings_cache() 失效。"""
    return load_settings()


def reset_settings_cache() -> None:
    """清空 get_settings 缓存（主要供测试使用）。"""
    get_settings.cache_clear()
