# intellect — Agent 知识库系统

以 SQLite 为唯一事实数据源、FTS5 + jieba 与向量检索混合召回的个人知识库，
覆盖 **文档导入 → 知识抽取 → 混合检索 → 问答引用 → 审核 / 纠错 → 版本回滚** 完整闭环。

- 权威设计稿：[document/DESIGN.md](document/DESIGN.md)（V0.3）
- 表结构准绳：[document/Agent知识库-建表脚本V0.3.sql](document/Agent知识库-建表脚本V0.3.sql)
- 协作规范：[document/AGENT.md](document/AGENT.md) · 任务清单：[document/TASK.md](document/TASK.md)

## 环境要求

- Python **3.11 ~ 3.13**（SQLite 驱动使用标准库 `sqlite3`，无需额外安装）
- [uv](https://docs.astral.sh/uv/)（推荐）：未安装可 `pip install uv`，uv 会自动下载所需 Python 解释器

## 快速开始（一条命令）

```bash
uv sync && uv run intellect
```

`uv sync` 按锁文件 `uv.lock` 一键安装全部依赖并创建 `.venv`；`uv run intellect` 运行空入口，
输出骨架就绪信息即表示环境可用。

<details>
<summary>无 uv 时的等价操作（pip）</summary>

```bash
python -m venv .venv
.venv/Scripts/pip install -e .        # Windows；Linux/macOS 为 .venv/bin/pip
.venv/Scripts/intellect
```

</details>

## 配置

配置中心默认值内置于代码（与设计稿一致），可通过 `config/config.toml` 覆盖：
复制 [`config/config.example.toml`](config/config.example.toml) 为 `config/config.toml` 后按需修改；
API Key 一律经环境变量注入，不写入配置文件。
配置文件路径可用环境变量 `INTELLECT_CONFIG` 指定。

## 运行测试

```bash
uv run pytest
```

## 目录结构

与设计稿 2.2 七大模块一一对应，详见 [document/AGENT.md](document/AGENT.md) 第二节。
