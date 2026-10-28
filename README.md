# intellect — Agent 知识库系统

以 SQLite 为唯一事实数据源、FTS5 + jieba 与向量检索混合召回的个人知识库，
覆盖 **文档导入 → 知识抽取 → 混合检索 → 问答引用 → 审核 / 纠错 → 版本回滚** 完整闭环。

- 权威设计稿：[document/DESIGN.md](document/DESIGN.md)（V0.9）
- 表结构准绳：[document/Agent知识库-建表脚本V0.9.sql](document/Agent知识库-建表脚本V0.9.sql)
- 协作规范：[AGENTS.md](AGENTS.md) · 任务清单：[document/TASK.md](document/TASK.md)

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

初始化会创建 V0.9 的 16 张业务表与 `knowledge_fts`，启用 WAL，并原子提交建表。
V0.9 新增任务抽取配置、候选标题/输出序号与 `extraction_result` 成功检查点。
V0.8 及以前事实库不会自动迁移；抽取服务遇到旧结构会明确报错，须另行评审迁移。
V0.7 新增任务批次号、解析配置快照和切片完成数量，文档首次发布前有效批次为空。已有 V0.4–V0.6 数据库不会自动迁移；批次服务遇到旧结构会明确报错，不可通过重建事实库升级。
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
当前采用显式维护流程；未接入自动启动核验。FTS 配置与下述业务哈希协议独立。

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

与设计稿 2.2 七大模块一一对应，详见 [AGENTS.md](AGENTS.md) 第一节。


## 导入批次基础（T2.8）

`intellect.doc.batches.DocumentBatchService` 使用已有连接和已登记的文档 ID：

- `create(document_id, parser=..., parser_version=..., chunking=...)` 分配文档内递增批次并保存解析配置。`chunking` 省略时从配置中心读取；同文档已有未发布批次时返回错误及原任务 ID，应恢复原任务。
- `get(task_id).parse_config` 是不可变 JSON 快照。调用方必须使用快照对应的解析器版本、参数和原始归档文件；失败重试复用原任务，不重新 create。
- `save_chunks(task_id, [ChunkInput(...)])` 原子保存一组切片；已存在切片须原文、哈希、token_count 和位置元数据一致，才复用原切片 ID。上次已提交内容不受本次失败影响。
- 只有解析成功读完输入后，才调用 `publish(task_id, expected_count=N)`。服务验证完整序号与哈希，再原子切换有效批次及配置；成功空结果用 N=0。不能用已写入行数推断解析完成。

独立调用使用短写事务；外层已开启事务时使用保存点，提交/回滚由调用方决定。发布不改变整个导入任务状态，旧批次及配置继续保留。切片哈希采用随包 `hash_protocols.json` 固定的 chunk-v1 协议，保留所有原文字符；文件/正文/知识哈希及实际解析器已接入，候选抽取见 T2.4，worker 尚待接入。

## 文档解析与切片（T2.1 / T2.2）

`inspect_bytes(data, source_type)`（位于 `intellect.doc.parsers`）提取完整正文并返回
`file-v1` 原文件哈希、`document-v1` 正文哈希及文件大小，供上游登记/文件去重使用。
处理服务使用已登记的文档 ID，不新增文档或跳过 T3.2 的文件去重规则：

```python
from contextlib import closing
from intellect.db import connect_database
from intellect.doc.processing import DocumentProcessingService

with closing(connect_database()) as connection:
    service = DocumentProcessingService(connection)
    batch = service.create(document_id)  # document_id 来自文档登记
    published = service.run(batch.task_id)
    # 中断后仍调用 run(batch.task_id)，不要再次 create 分配新任务
```

支持 TXT 严格按显式编码解码、PDF 文本层、DOCX 主文档段落/表格（含嵌套表格），以及本地归档 HTML。
PDF 加密文件及无文本层的图像页明确报错，当前不提供 OCR；HTML 不执行 JavaScript，排除 script/style/template 内容。
WEB 可先调用 `intellect.doc.web.archive_web(url, destination)` 归档实际响应字节，返回路径、原始哈希和响应编码；
以返回编码创建 WEB 任务（`parser_options={"encoding": archive.encoding}`）。已有归档禁止覆盖，重试不再次访问远端。

`[document]` 统一配置默认 `text_encoding` 和归档请求的 `web_timeout_seconds`。
任务快照冻结本地归档 URI/字节哈希、解析器及后端版本、编码、切分器和词元统计器版本；
文件字节改变或版本不一致时拒绝继续旧批次。解析配置在切片前持久化，运行不改写文档登记时的文件/正文哈希。

切片按配置的字符长度、重叠与分隔符优先级切分，保留原文字符，不跨 PDF 页或 DOCX 段落/单元格重叠；
位置包含解析正文中的字符范围（Unicode 码点）、行号及可用页/段落/表格路径。
`token_count` 固定使用随包 jieba 0.42.1 词典、HMM=False 的非纯空白词元数，不是 LLM 计费 token，亦不受 FTS 配置影响。
只有解析和切分完整结束后才发布最终切片数；中途失败保留已提交切片与旧有效批次，完整空结果发布为 0。
固定词典保留其 [MIT 许可声明](src/intellect/doc/assets/JIEBA_LICENSE.txt)，原始字节由 Git 属性保护，不做换行转换。

## 固定知识正文哈希（T2.9）

`intellect.knowledge.hashing.knowledge_hash(content)` 为候选比较和正式版本写入提供共用的
`knowledge-v1:sha256:…` 协议；输入为完整正文，不含标题，仅将 CRLF / CR 统一为 LF。
独立 jieba 0.42.1 实例读取随包固定词典，关闭 HMM，保留空白与标点等全部词元，
按固定 JSON 数组序列化后计算 SHA-256；不使用 FTS 用户词典或停用词，不改写保存的正文。

`get_knowledge_hasher().manifest` 提供协议与资产指纹副本；使用前检查分词版本、词典和清单，
不匹配时明确报错。file/document/chunk/knowledge-v1 四种协议清单均随包发布。
本接口不回填或覆盖历史哈希；首次升级业务哈希规则或专用资产前，仍须评审兼容方案。

## 文档登记与文件去重（T3.2）

新来源使用登记服务，先检查启用文档的正文哈希，再比较命中文档的原文件哈希：

```python
from contextlib import closing
from intellect.db import connect_database
from intellect.doc.importing import DocumentImportService

with closing(connect_database()) as connection:
    service = DocumentImportService(connection)
    registered = service.register("data/archive/source.txt", title="资料", kind="TXT")
    published = service.processing.run(registered.batch.task_id)
```

正文相同默认抛出 `DuplicateDocumentError`，`context["documents"]` 提供已有文档 ID、标题、
归档位置和原文件是否相同；拒绝时不新增文档或任务。用户明确选择保留独立来源后，
以 `confirmed_duplicate=True` 再次登记，保留各自文档、任务与证据切片。
WEB 先归档，再传入本地路径、`kind="WEB"` 及响应编码对应的 `parser_options`。

文档与初始批次同事务登记，并发去重在写事务内复核；批次准备失败整体回滚。
独立调用自行提交，外层事务由调用方提交或回滚。重试处理使用原任务 ID；重新解析同一文档时，
使用 `DocumentProcessingService.create(existing_document_id)`，不重新登记来源。
当前提供 Python 服务接口，入口尚未接入导入 UI、worker 或拒绝审计；可将已发布批次交给下述抽取服务。

## LangChain 候选抽取（T2.4）

将调用方配置好的 LangChain 模型 Runnable 注入适配器，并明确实际模型名称与版本：

```python
from intellect.extract.model import LangChainExtractor
from intellect.extract.service import ExtractionService

# model 是已配置的 LangChain Runnable；提供商、名称与版本须与实际模型一致
extractor = LangChainExtractor(model, provider=provider, model_name=model_name, model_version=model_version)
results = ExtractionService(connection, extractor).run(published.task_id)
```

版本化提示词要求每次完整输出 `{"items":[{"title":"标题","content":"完整正文"}]}`；
没有知识时为 `{"items":[]}`。标题与正文必须非空，保留适用条件、例外和上下文；
缺标题、截断 JSON、结构不符或调用失败均报 `ExtractionError`，不补标题或保存部分输出。
未新增提供商客户端或自动选定模型，API Key 不进入数据库配置快照；真实模型调用尚未验收。

仅处理完整发布的固定批次。模型调用前持久化实际模型/版本及提示词/结构版本与指纹；
后续调用必须匹配快照。模型在写事务之外调用，提交时复核来源内容、哈希、元数据及任务配置。
同一切片全部候选和成功检查点同事务提交；成功空结果保存数量 0，失败不留成功标记。
重试核对完整集合后复用原候选 ID，跳过模型；并发只保留先成功的完整集合。
记录损坏、候选缺检查点或配置不一致时报错，不自动覆盖。

`run` 需要在调用方写事务外执行。需要组合外层事务时，可先 `prepare(task_id)` 得到来源快照，
在事务外用 `extractor.invoke(source.content)` 获取输出，再于外层事务内
`save_result(source, output)`；配置准备和结果提交均支持保存点，最终提交/回滚由调用方决定。
候选保持待审核；本服务不改变任务状态，不创建正式知识、审核决策或索引同步任务。
