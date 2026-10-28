-- 运行时 DDL：来源 document/Agent知识库-建表脚本V0.8.sql（DESIGN V0.8）
-- PRAGMA 由连接工厂与初始化入口执行；不自动迁移已有事实库。
-- V0.8：切片词元统计口径见 DESIGN §3.3；DDL 与 V0.7 完全一致。
-- V0.7：任务固定批次/配置，chunk_count 完整发布标记；仅新库初始化，旧事实库迁移另行评审。
-- V0.6：初始哈希协议见 DESIGN §5.2；与 V0.5 DDL 一致，不需要结构迁移。
-- 哈希 TEXT 值保存 <用途>-v1:sha256:<64位小写摘要>，用途为 file/document/chunk/knowledge。
-- 知识哈希使用固定分词资产，与 FTS 配置隔离；首次写入前交付协议清单与快照。
-- V0.5：分类/别名规则见 DESIGN §3.8；与 V0.4 DDL 完全一致，不需要结构迁移。
-- ============================================================================
-- Agent 知识库系统 建表脚本 V0.8
-- V0.5：别名按统一比较规则保证同对象唯一；不新增停用字段。
-- 执行前：每个连接注册 INTELLECT_ALIAS_V1，精确定义见 DESIGN §3.10。
-- 本脚本用于新库初始化，不是 V0.3 事实库迁移脚本；禁止用重建事实库代替迁移。
-- 表清单：
--   embedding_model   向量模型注册表                    [决策 5a]
--   source_document   原始文档表                        [决策 10/13/6]
--   document_chunk    原文切片表                        [决策 10]
--   knowledge         正式知识表（含向量同步进度跟踪）    [决策 5a]
--   knowledge_version 知识版本表                        [决策 3/8/6]
--   knowledge_source  知识来源表                        [决策 12]
--   category          分类表
--   knowledge_category 知识分类关联表
--   alias             别名表
--   task_process      通用任务表（导入 + 索引同步）       [决策 1/9]
--   candidate         候选知识表                         [决策 15]
--   review_issue      审核事项表                         [决策 6/11]
--   change_log        变更日志表                         [决策 6]
--   qa_record         问答记录表                         [决策 2]
--   qa_citation       回答引用快照表                     [决策 2]
--   knowledge_fts     FTS5 全文索引（虚拟表）            [决策 4/9]
-- ============================================================================



-- ============================================================================
-- 1. embedding_model 向量模型注册表
-- ============================================================================
CREATE TABLE IF NOT EXISTS embedding_model (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  model_name  TEXT    NOT NULL UNIQUE,       -- 模型标识，建议含版本，如 'bge-m3'
  dimension   INTEGER NOT NULL,              -- 向量维度
  is_active   INTEGER NOT NULL DEFAULT 0,    -- 1 = 当前启用；由下面的部分唯一索引保证唯一
  create_time TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 全表至多一条 is_active=1
CREATE UNIQUE INDEX IF NOT EXISTS uk_embedding_model_active
  ON embedding_model (is_active) WHERE is_active = 1;

-- ============================================================================
-- 2. source_document 原始文档表
-- ============================================================================
CREATE TABLE IF NOT EXISTS source_document (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  title            TEXT    NOT NULL,
  storage_url      TEXT    NOT NULL,         -- 原始文件存储位置
  source_type      TEXT,                     -- PDF / TXT / DOCX / WEB ...
  file_hash        TEXT,                     -- 原始文件哈希
  content_hash     TEXT    NOT NULL,         -- 规范化正文哈希（不设 UNIQUE：不同文档允许同文，见去重规则）
  file_size        INTEGER,
  parse_config     TEXT,                     -- 当前已发布配置副本；历史快照在 task_process
  chunk_batch      INTEGER,                    -- 当前已发布有效批次，首次发布前 NULL
  uploader_id      TEXT,                     -- 操作者占位 [决策 6]，系统操作写 'system'
  import_time      TEXT    NOT NULL DEFAULT (datetime('now')),
  status           INTEGER NOT NULL DEFAULT 0 -- 0 启用 1 停用（停用不级联删除知识/切片/审核记录）
);

CREATE INDEX IF NOT EXISTS idx_source_document_content_hash ON source_document (content_hash);
CREATE INDEX IF NOT EXISTS idx_source_document_file_hash    ON source_document (file_hash);


-- ============================================================================
-- 3. document_chunk 原文切片表
-- ============================================================================
CREATE TABLE IF NOT EXISTS document_chunk (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  source_document_id INTEGER NOT NULL REFERENCES source_document(id),
  chunk_batch        INTEGER NOT NULL DEFAULT 1,     -- 任务绑定批次，可不同于文档当前有效批次
  chunk_index        INTEGER NOT NULL,               -- 片段在文档中的顺序
  content            TEXT    NOT NULL,               -- 原文片段内容（证据，非规范化知识）
  chunk_hash         TEXT    NOT NULL,               -- 规范化内容哈希，幂等复用判断
  token_count        INTEGER,                        -- 词元数
  location_meta      TEXT,                           -- JSON：页码/章节/段落
  create_time        TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (source_document_id, chunk_batch, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_document_chunk_hash ON document_chunk (chunk_hash);


-- ============================================================================
-- 4. knowledge 正式知识表
-- ============================================================================
CREATE TABLE IF NOT EXISTS knowledge (
  id                         INTEGER PRIMARY KEY AUTOINCREMENT,
  current_version_id         INTEGER REFERENCES knowledge_version(id), -- 当前有效版本
  status                     INTEGER NOT NULL DEFAULT 0,              -- 0 有效 1 停用
  indexed_version_id         INTEGER REFERENCES knowledge_version(id), -- 向量索引已同步到的版本
  indexed_embedding_model_id INTEGER REFERENCES embedding_model(id),   -- 向量索引所用模型
  create_time                TEXT    NOT NULL DEFAULT (datetime('now')),
  update_time                TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_knowledge_status ON knowledge (status);


-- ============================================================================
-- 5. knowledge_version 知识版本表
-- ============================================================================
CREATE TABLE IF NOT EXISTS knowledge_version (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  knowledge_id  INTEGER NOT NULL REFERENCES knowledge(id),
  version_no    INTEGER NOT NULL,              -- 该知识点内的版本序号，只增不减（回滚也是新版本）
  title         TEXT,                          -- 标题
  content       TEXT    NOT NULL,              -- 完整规范化知识内容（适用条件/例外包含在正文内）
  content_hash  TEXT    NOT NULL,              -- 规范化哈希，知识级去重的可索引依据
  change_type   INTEGER NOT NULL,              -- 0 新建 1 补充合并 2 纠错 3 回滚
  ref_type      INTEGER,                       -- 触发来源：0 candidate 1 review_issue 2 qa_record；NULL = 无
  ref_id        INTEGER,                       -- 触发来源记录 ID（与 ref_type 配对，无法建外键，业务层保证）
  change_reason TEXT,
  created_by    TEXT,                          -- 操作者占位，系统操作写 'system'
  create_time   TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (knowledge_id, version_no)            -- 历史版本创建后不被覆盖
);

CREATE INDEX IF NOT EXISTS idx_knowledge_version_hash ON knowledge_version (content_hash);


-- ============================================================================
-- 6. knowledge_source 知识来源表
-- ============================================================================
CREATE TABLE IF NOT EXISTS knowledge_source (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  knowledge_id INTEGER NOT NULL REFERENCES knowledge(id),
  chunk_id     INTEGER NOT NULL REFERENCES document_chunk(id),
  source_role  INTEGER NOT NULL DEFAULT 0,     -- 0 直接支持 1 补充说明 2 反例
  create_time  TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (knowledge_id, chunk_id, source_role)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_source_chunk ON knowledge_source (chunk_id);


-- ============================================================================
-- 7. category 分类表（树状自关联）
-- ============================================================================
CREATE TABLE IF NOT EXISTS category (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id INTEGER REFERENCES category(id),
  name      TEXT    NOT NULL,
  status    INTEGER NOT NULL DEFAULT 0         -- 0 启用 1 停用
);


-- ============================================================================
-- 8. knowledge_category 知识分类关联表
-- ============================================================================
CREATE TABLE IF NOT EXISTS knowledge_category (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  knowledge_id INTEGER NOT NULL REFERENCES knowledge(id),
  category_id  INTEGER NOT NULL REFERENCES category(id),
  UNIQUE (knowledge_id, category_id)
);


-- ============================================================================
-- 9. alias 别名表
--    待审别名不写本表：进入 review_issue.suggestion_payload，审核通过后写入
-- ============================================================================
CREATE TABLE IF NOT EXISTS alias (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_type  INTEGER NOT NULL,                -- 0 知识 1 分类（多态关联，无法建外键，业务层保证）
  owner_id    INTEGER NOT NULL,
  name        TEXT    NOT NULL,                -- 写入 FTS 前统一做规范化处理
  origin      INTEGER NOT NULL DEFAULT 0,      -- 0 文档 1 用户
  create_time TEXT    NOT NULL DEFAULT (datetime('now'))
);


-- 比较键：str.casefold()；原串恰好一个 U+0020 时删除它，否则保留全部空格。
-- 其他空白不裁剪/压缩；按键的 Unicode 字符顺序比较。原始 name 保留展示。
-- 业务查询也须显式使用 INTELLECT_ALIAS_V1；origin 不参与唯一性。
CREATE UNIQUE INDEX IF NOT EXISTS uk_alias_owner_name
  ON alias (owner_type, owner_id, name COLLATE INTELLECT_ALIAS_V1);

-- ============================================================================
-- 10. task_process 通用任务表
--     task_type = 0 文档导入：状态机 待处理→处理中→{待审核|已完成|已失败}，source_document_id 必填
--     task_type = 1 索引同步：状态机 待处理→处理中→{已完成|已失败}，永不使用"待审核"，
--                knowledge_id + version_id 必填；消费前校验 version_id 仍为该知识当前版本，
--                防止旧任务覆盖新版本的向量数据
-- ============================================================================
CREATE TABLE IF NOT EXISTS task_process (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  task_type          INTEGER NOT NULL,          -- 0 文档导入 1 索引同步
  -- 文档导入任务专用
  source_document_id INTEGER REFERENCES source_document(id),
  chunk_batch        INTEGER CHECK (chunk_batch > 0), -- 固定批次；同步任务 NULL
  parse_config       TEXT,                       -- 不可变解析配置快照
  chunk_count        INTEGER CHECK (chunk_count >= 0), -- NULL 未发布，0 成功空批次
  -- 索引同步任务专用
  knowledge_id       INTEGER REFERENCES knowledge(id),
  version_id         INTEGER REFERENCES knowledge_version(id),
  operation          INTEGER,                   -- 0 更新 1 删除，索引操作类型，针对知识删除又撤销的情况
  -- 通用
  retry_count        INTEGER NOT NULL DEFAULT 0,
  status             INTEGER NOT NULL DEFAULT 0, -- 0 待处理 1 处理中 2 待审核 3 已完成 4 已失败
  error_info         TEXT,
  create_time        TEXT    NOT NULL DEFAULT (datetime('now')),
  update_time        TEXT    NOT NULL DEFAULT (datetime('now')),
  start_time         TEXT,
  finish_time        TEXT
);

-- 后台轮询队列索引
CREATE INDEX IF NOT EXISTS idx_task_process_queue ON task_process (task_type, status);

-- 一个文档批次只属于一个导入任务；失败未发布仍占用该文档的解析槽。
CREATE UNIQUE INDEX IF NOT EXISTS uk_task_document_batch
  ON task_process (source_document_id, chunk_batch) WHERE task_type = 0;
CREATE UNIQUE INDEX IF NOT EXISTS uk_task_document_unpublished
  ON task_process (source_document_id) WHERE task_type = 0 AND chunk_count IS NULL;


-- ============================================================================
-- 11. candidate 候选知识表
-- ============================================================================
CREATE TABLE IF NOT EXISTS candidate (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  content         TEXT    NOT NULL,            -- 候选知识完整文本（历史抽取结果，不随正式知识修改而变化）
  task_process_id INTEGER NOT NULL REFERENCES task_process(id),
  chunk_id        INTEGER NOT NULL REFERENCES document_chunk(id),
  status          INTEGER NOT NULL DEFAULT 1,  -- 0 已处理 1 待审核 2 已拒绝
  extract_model   TEXT,                        -- 抽取模型标识
  create_time     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_candidate_task ON candidate (task_process_id);


-- ============================================================================
-- 12. review_issue 审核事项表
-- ============================================================================
CREATE TABLE IF NOT EXISTS review_issue (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  knowledge_id         INTEGER REFERENCES knowledge(id),          -- 涉及的正式知识；全新知识可为空
  candidate_id         INTEGER NOT NULL REFERENCES candidate(id),
  issue_type           INTEGER NOT NULL,           -- 0 重复 1 冲突 2 可补充 3 无法判断
  status               INTEGER NOT NULL DEFAULT 0, -- 0 待审核 1 已解决 2 已驳回/取消
  suggestion_action_type    INTEGER,                    -- 系统建议操作类型（0 合并来源 / 1 创建新知识 / 2 补充合并新版本 / 3 采纳新内容替换 / 4 拒绝候选 / 5 无法判断）
  suggestion_payload   TEXT,                       -- JSON 建议载荷；高分合并存逐项核对、模型、比较版本与原文证据（DESIGN §5.2）
  knowledge_version_id INTEGER REFERENCES knowledge_version(id), -- 审核针对的版本快照
  resolution           TEXT,                       -- 最终决策；高分自动合并须注明相似度及一致性核对通过依据
  resolved_by          TEXT,                       -- 操作者占位；自动操作写 'system'
  create_time          TEXT    NOT NULL DEFAULT (datetime('now')),
  resolve_time         TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_issue_status ON review_issue (status);


-- ============================================================================
-- 13. change_log 变更日志表
-- ============================================================================
CREATE TABLE IF NOT EXISTS change_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type   INTEGER NOT NULL,               -- 0 分类 1 知识（建议扩展：2 文档 3 别名 4 任务）
  target_id     INTEGER NOT NULL,
  old_value     TEXT,                           -- 修改前快照
  new_value     TEXT,                           -- 修改后快照
  change_reason TEXT,
  operator_id   TEXT,                           -- 操作者占位；系统操作写 'system'
  create_time   TEXT    NOT NULL DEFAULT (datetime('now'))
);


-- ============================================================================
-- 14. qa_record 问答记录表
-- ============================================================================
CREATE TABLE IF NOT EXISTS qa_record (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  question         TEXT    NOT NULL,
  answer           TEXT    NOT NULL,
  model            TEXT,                        -- 回答模型，便于归因
  feedback_status  INTEGER NOT NULL DEFAULT 0,  -- 0 无反馈 1 有效 2 有误
  feedback_type    INTEGER,                     -- 0 模型回答错误 1 检索错误 2 知识缺失 3 知识内容错误/过期
  feedback_content TEXT,
  create_time      TEXT    NOT NULL DEFAULT (datetime('now'))
);


-- ============================================================================
-- 15. qa_citation 回答引用快照表
-- ============================================================================
CREATE TABLE IF NOT EXISTS qa_citation (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  qa_record_id INTEGER NOT NULL REFERENCES qa_record(id),
  knowledge_id INTEGER NOT NULL REFERENCES knowledge(id),
  version_id   INTEGER NOT NULL REFERENCES knowledge_version(id),  -- 引用发生时的版本
  chunk_id     INTEGER REFERENCES document_chunk(id),
  score        REAL,                            -- 召回得分
  source       TEXT                             -- 'fts5' / 'vector' / 'both'
);

CREATE INDEX IF NOT EXISTS idx_qa_citation_record    ON qa_citation (qa_record_id);
CREATE INDEX IF NOT EXISTS idx_qa_citation_knowledge ON qa_citation (knowledge_id);


-- ============================================================================
-- 16. knowledge_fts FTS5 全文索引 [决策 4 jieba 预分词 + 决策 9 随业务事务更新]
--
--     使用约定：
--     (1) jieba 预分词：应用层对标题/正文/别名/分类名分词后以空格拼接写入 *_tokens 列，
--         tokenizer 用默认 unicode61（按空格切分即可）。查询时对问题分词、去停用词后以显式 OR 拼接进 MATCH
--         （FTS5 空格分隔为隐式 AND，多词直拼会零召回，见 DESIGN §7.3 查询方案）。
--     (2) 行约定 rowid = knowledge_id，一个知识一行，vector 主键同用 knowledge_id；
--         upsert 即 DELETE + INSERT：
--           DELETE FROM knowledge_fts WHERE rowid = :knowledge_id;
--           INSERT INTO knowledge_fts (rowid, title_tokens, content_tokens,
--                                     alias_tokens, category_tokens,
--                                     knowledge_id, version_id)
--           VALUES (:knowledge_id, ...);
--     (3) 一致性 [决策 9]：知识创建/版本变更/停用/删除时，FTS 行的维护与正式数据写入放在
--         同一个业务事务内完成（FTS5 是 SQLite 虚拟表，可事务更新）；
--         只有向量索引异步走 task_process(task_type=1)。
--     (4) jieba 版本与词典必须写入/查询两侧一致；升级词典需整体重建本表（PROBLEM.MD 问题 7）。
--
--     检索示例（应用层先对问题分词、去停用词，再显式 OR 拼接；禁止空格直拼——隐式 AND 会零召回）：
--       SELECT knowledge_id FROM knowledge_fts
--        WHERE knowledge_fts MATCH '原子性 OR 可见性'
--        ORDER BY rank LIMIT 20;   -- rank = bm25：命中词越多、词越稀有者排前
-- ============================================================================
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
  title_tokens,          -- 标题分词结果（来自 knowledge_version.title）[决策 3]
  content_tokens,        -- 正文分词结果（当前有效版本 content）
  alias_tokens,          -- 别名分词结果（经确认的 alias）
  category_tokens,       -- 分类名分词结果
  knowledge_id UNINDEXED,
  version_id   UNINDEXED,
  tokenize = 'unicode61'
);


-- ============================================================================
-- 初始化完成。建议的首次初始化步骤：
--   1. 注册 INTELLECT_ALIAS_V1 后执行本脚本建表；
--   2. INSERT INTO embedding_model(...) 启用一个向量模型；
--   3. INSERT INTO category(...) 建立初始分类树。
-- ============================================================================
