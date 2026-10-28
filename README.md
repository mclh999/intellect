# intellect — Agent 知识库系统

以 SQLite 为唯一事实数据源、FTS5 + jieba 与向量检索混合召回的个人知识库，
覆盖 **文档导入 → 知识抽取 → 混合检索 → 问答引用 → 审核 / 纠错 → 版本回滚** 完整闭环。

- 权威设计稿：[document/DESIGN.md](document/DESIGN.md)（V0.7）
- 表结构准绳：[document/Agent知识库-建表脚本V0.7.sql](document/Agent知识库-建表脚本V0.7.sql)
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

初始化会创建 V0.7 的 15 张业务表与 `knowledge_fts`，启用 WAL，并原子提交建表。
V0.7 增加任务批次快照、切片完成数量与唯一索引，首次发布前文档有效批次为空。
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

## 分词资产与 FTS 重建

写入和查询共用 `intellect.search.tokenization`。默认使用锁定的 jieba 内置词典和随包发布的空停用词表。
可在 `[tokenization]` 配置 `user_dictionaries`（按顺序加载的 UTF-8 jieba 词典路径）及
`stopwords_path`（UTF-8，一行一个精确词元）；相对路径以工作目录为基准。
自定义词典支持省略频次或填写正整数频次；零频词不受支持，以避免修改 jieba 的全局 HMM 状态。

`get_tokenizer().manifest` 提供 jieba 版本、内置/自定义词典与停用词文件的 SHA-256，
`manifest.asset_id` 标识整组资产。词典、停用词及清单须随同一发布版本保存。
在用实例持有文件内容快照，修改磁盘文件不会改变该实例；停用词仅过滤查询词元，不改写正式正文。

升级 jieba、词典或停用词时，先暂停业务读写，再使用新资产在维护进程中执行：

```python
from contextlib import closing
from intellect.db import connect_database
from intellect.index.fts import rebuild_fts

with closing(connect_database()) as connection:
    count = rebuild_fts(connection)
```

重建成功后再用相同配置与资产重启服务。失败时原 FTS 整体回滚，保持停服或恢复旧资产后重启。
重建只读取正式知识当前版本、别名和分类，不改写事实记录或创建向量任务。
当前采用显式维护流程；未接入自动启动核验，也未实现知识/切片业务哈希协议。

## FTS 关键词检索

```python
from contextlib import closing
from intellect.db import connect_database
from intellect.search.fts import FtsSearchService

with closing(connect_database()) as connection:
    hits = FtsSearchService(connection).search("原子性 可见性")
```

去停用词后不超过 `fts.short_question_max_terms` 的问题，逐词转义并以显式 OR 检索，
按 bm25（`rank`）取 `fts.top_k`。返回知识/版本 ID、当时标题/正文及原始 rank，过滤停用知识和过期 FTS 版本。

长问题须向服务传入 `concept_extractor(question)`，返回 2–4 个非空文本概念；服务逐概念取 top-K，
按召回顺序去重合并，不跨概念相加或重排 rank。缺少抽取器、数量非法或调用失败均抛出 `FtsSearchError`。
当前只提供抽取器接口，真实模型、意图理解和完整问答流程仍由 T4 接入。
多个概念的数据库查询共用读快照，不提交调用方已有事务；空问题或全停用词问题返回空列表。

## 运行测试

```bash
uv run pytest
```

## 目录结构

与设计稿 2.2 七大模块一一对应，详见 [document/AGENT.md](document/AGENT.md) 第二节。

## 导入批次基础（T2.8）

`intellect.doc.batches.DocumentBatchService` 使用已有连接和已登记的文档 ID：

- `create(document_id, parser=..., parser_version=..., chunking=...)` 分配文档内递增批次并保存解析配置。`chunking` 省略时从配置中心读取；同文档已有未发布批次时返回错误及原任务 ID，应恢复原任务。
- `get(task_id).parse_config` 是不可变 JSON 快照。调用方必须使用快照对应的解析器版本、参数和原始归档文件；失败重试复用原任务，不重新 create。
- `save_chunks(task_id, [ChunkInput(...)])` 原子保存一组切片；已存在切片须原文、哈希、token_count 和位置元数据一致，才复用原切片 ID。上次已提交内容不受本次失败影响。
- 只有解析成功读完输入后，才调用 `publish(task_id, expected_count=N)`。服务验证完整序号与哈希，再原子切换有效批次及配置；成功空结果用 N=0。不能用已写入行数推断解析完成。

独立调用使用短写事务；外层已开启事务时使用保存点，提交/回滚由调用方决定。发布不改变整个导入任务状态，旧批次及配置继续保留。切片哈希采用随包 `hash_protocols.json` 固定的 chunk-v1 协议，保留所有原文字符；文件/正文/知识哈希、实际解析器、候选抽取和 worker 仍待对应任务接入。
