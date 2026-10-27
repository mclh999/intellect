# intellect — Agent 知识库系统

以 SQLite 为唯一事实数据源、FTS5 + jieba 与向量检索混合召回的个人知识库，
覆盖 **文档导入 → 知识抽取 → 混合检索 → 问答引用 → 审核 / 纠错 → 版本回滚** 完整闭环。

- 权威设计稿：[document/DESIGN.md](document/DESIGN.md)（V0.5）
- 表结构准绳：[document/Agent知识库-建表脚本V0.5.sql](document/Agent知识库-建表脚本V0.5.sql)
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
文件中出现 `llm.api_key` / `embedding.api_key` 将报错；配置对象的文本表示与导出均隐藏密钥。

## 数据库初始化

首次创建空数据库（路径来自 `database.path`，默认 `data/intellect.db`）：

```bash
uv run python -c "from intellect.db import initialize_database; initialize_database()"
```

初始化会创建 V0.5 的 15 张业务表与 `knowledge_fts`，启用 WAL，并原子提交建表。
V0.5 与 V0.4 表结构一致，本次只补齐分类管理业务规则。
目标库已有结构时会报错，已有事实库迁移须先评审；初始化失败不会删除数据库文件。
运行时 SQL 随 Python 包发布，无需依赖本地 `document/` 目录。

业务代码通过 `intellect.db.connect_database()` 连接已有库；每连接注册
`INTELLECT_ALIAS_V1` 并设置外键、5 秒锁等待与 `synchronous=NORMAL`。
调用方负责事务与关闭连接。Embedding 模型登记暂缓，主入口仍为骨架入口。

## 分类与已确认别名

分类与别名管理通过 Python 服务调用，可按实际需要输入初始分类树：

```python
from contextlib import closing
from intellect.db import connect_database
from intellect.knowledge import AliasService, CategorySeed, CategoryService

with closing(connect_database()) as connection:
    categories = CategoryService(connection)
    root, child = categories.create_tree((
        CategorySeed("工作资料", (CategorySeed("数据库"),)),
    ))
    AliasService(connection).add(1, child.id, "DB", confirmed=True)
```

示例分类不会自动安装。服务支持分类读取、建树、改名、移动、启停、删除与知识关联；
改名、移动、启停和删除须填写 `reason`，可用 `operator` 记录操作者。
移动禁止自指和成环，启停仅改变本节点；有子节点（含停用节点）或知识关联时禁止删除。
待审别名不写入正式表，同对象重复添加复用已有别名，误加别名可直接删除。

直接关联的启用分类名称及已确认别名进入 FTS；分类/别名与受影响 FTS 在同一事务更新。
知识别名变更也同事务维护 FTS，不创建知识新版本或向量任务。
服务独立调用时自行提交；若调用方已开启事务，服务使用保存点并由调用方最终提交/回滚。

## 运行测试

```bash
uv run pytest
```

## 目录结构

与设计稿 2.2 七大模块一一对应，详见 [document/AGENT.md](document/AGENT.md) 第二节。
