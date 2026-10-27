# CiteFlow 架构说明

> 目标：把「导入资料 → 混合检索 → 回到原文页 → 记住偏好与进度」做成一个可离线运行的研读闭环，并让 seekdb 与 PowerContext 各自承担真实、可核对的功能。
>
> 验证环境：seekdb 1.4.0.0 · PowerContext 1.2.0 · Python 3.12.3 · fastembed 0.9.1。

## 一、总览

```text
┌──────────────────────────────────────────────────────────────────┐
│ 浏览器（原生 HTML/CSS/JS，无构建步骤）                            │
│ 书架 · 提问 · 引用/页图 · 记忆 · 技术计划                         │
└──────────────────────────────┬───────────────────────────────────┘
                               │ /api/*
┌──────────────────────────────▼───────────────────────────────────┐
│ FastAPI（app/server.py）                                          │
│                                                                  │
│  ingest.py       PDF/MD/TXT → 页/节 → 900 字切块 → 批量嵌入      │
│  embeddings.py   fastembed 多语言模型 / 确定性 hash 降级          │
│  retrieval.py    单条混合 SQL → RRF → 命中与计划证据              │
│  answer.py       证据抽取（默认）/ OpenAI 兼容 LLM（可选）        │
│  memory.py       PowerContext HTTP + seekdb 镜像 / 降级           │
│  db.py           pymysql、schema、查询日志、p50/p95               │
└───────────────┬───────────────────────────────┬──────────────────┘
                │ MySQL 协议                    │ HTTP
┌───────────────▼──────────────────┐  ┌─────────▼──────────────────┐
│ seekdb 1.4 · 127.0.0.1:2891      │  │ PowerContext 1.2           │
│                                  │  │ 127.0.0.1:17429            │
│ cf_documents                     │  │                            │
│ cf_chunks                        │  │ /v1/scopes                 │
│   ├─ FULLTEXT INDEX (ik)         │  │ /v1/memory/remember        │
│   └─ VECTOR INDEX (hnsw/vsag)    │  │ /v1/memory/search          │
│ cf_queries                       │  │ /v1/context/prepare        │
│ cf_memories（镜像 / 降级）        │  │                            │
│ cf_progress（最后阅读页）         │  │ scope: spc_...             │
└──────────────────────────────────┘  └────────────────────────────┘
```

**seekdb 的职责**：保存文档元数据、可检索片段与 384 维向量；在同一条 SQL 中完成全文召回、向量召回和结构化过滤；保存每次查询的真实日志与延迟；镜像记忆，作为 PowerContext 停机时的降级路径。

**PowerContext 的职责**：保存跨会话的阅读偏好、结论和进度；提供 FTS 召回；用 `context.prepare` 生成注入回答的上下文材料；所有响应保留 `x-powercontext-request-id`。

## 二、数据模型（seekdb）

实现见 `app/db.py` 的 `init_schema()`。以下为实际执行的 DDL（省略 `IF NOT EXISTS`）：

```sql
CREATE TABLE cf_documents (
  id VARCHAR(64) PRIMARY KEY,
  collection_id VARCHAR(64) NOT NULL,
  title VARCHAR(512) NOT NULL,
  filename VARCHAR(512) NOT NULL,
  kind VARCHAR(16) NOT NULL,               -- pdf / markdown / text
  sha256 CHAR(64) NOT NULL,
  page_count INT NOT NULL,
  chunk_count INT NOT NULL,
  char_count INT NOT NULL,
  stored_path VARCHAR(1024) NOT NULL,
  created_at DATETIME NOT NULL,
  KEY cf_doc_collection (collection_id),
  KEY cf_doc_sha (sha256)
) ORGANIZATION = HEAP;

CREATE TABLE cf_chunks (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  document_id VARCHAR(64) NOT NULL,
  collection_id VARCHAR(64) NOT NULL,
  page_no INT NOT NULL,
  ordinal INT NOT NULL,
  heading VARCHAR(512) NOT NULL DEFAULT '',
  text MEDIUMTEXT NOT NULL,
  embedding VECTOR(384) NOT NULL,
  FULLTEXT INDEX cf_ft (text) WITH PARSER ik,
  VECTOR INDEX cf_vec (embedding)
    WITH (DISTANCE=l2, TYPE=hnsw, LIB=vsag, SYNC_MODE=IMMEDIATE),
  KEY cf_chunk_doc (document_id),
  KEY cf_chunk_collection (collection_id)
) ORGANIZATION = HEAP;

CREATE TABLE cf_queries (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  session_id VARCHAR(64) NOT NULL,
  query TEXT NOT NULL,
  mode VARCHAR(16) NOT NULL,               -- search / ask
  collection_id VARCHAR(64) NOT NULL,
  doc_filter VARCHAR(1024) NOT NULL DEFAULT '',
  latency_ms INT NOT NULL,
  hit_count INT NOT NULL,
  fts_hits INT NOT NULL,
  vector_hits INT NOT NULL,
  answer_mode VARCHAR(16) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL,
  KEY cf_query_time (created_at)
) ORGANIZATION = HEAP;

CREATE TABLE cf_memories (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  kind VARCHAR(32) NOT NULL,               -- preference / insight / progress
  text TEXT NOT NULL,
  backend VARCHAR(24) NOT NULL,            -- powercontext / seekdb
  pc_entry_id VARCHAR(128) NOT NULL DEFAULT '',
  pc_request_id VARCHAR(128) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  KEY cf_mem_user (user_id, updated_at)
) ORGANIZATION = HEAP;

CREATE TABLE cf_progress (
  user_id VARCHAR(64) PRIMARY KEY,
  document_id VARCHAR(64) NOT NULL,
  page_no INT NOT NULL,
  updated_at DATETIME NOT NULL
) ORGANIZATION = HEAP;
```

**设计取舍**

| 决策 | 原因 |
| --- | --- |
| 全文与向量索引同表 | 单条 SQL 可以直接对同一批行做双通道召回，避免跨表 ID 回填和二次过滤 |
| `SYNC_MODE=IMMEDIATE` | 交互式导入后应立即能搜到；实测异步模式存在短时间不可见窗口 |
| `cf_queries` 独立表 | 使用效果必须来自真实查询日志，不能由前端伪造或只在内存里统计 |
| `cf_memories` 仅作镜像 | PowerContext 是记忆主路径；镜像保证演示可在服务停机时继续，但 UI 会标注降级 |
| 每页保留 `page_no` | 回答引用必须能回到原页，PDF 还能渲染页图核对版式 |
| 切块目标 900 字、重叠 120 字 | 兼顾语义完整与检索精度；页内切块，不跨页拼接，避免引用页码含糊 |

## 三、入库链路

1. **解析**
   - PDF：`pypdf` 按页抽取文本；清理连字符换行、零宽字符和多余空白；保留页码；
   - Markdown：按 `#` 标题分节，节号映射为 `§n`，标题同时写入 chunk；
   - TXT：单节处理；文本型文件按 UTF-8 容错解码。
2. **去重**：对文件字节计算 SHA-256，已存在则直接返回原文档，不重复嵌入。
3. **切块**：按段落聚合到约 900 字，超出按句切分；块间保留 120 字尾部重叠；少于 40 字的块丢弃。
4. **嵌入**：fastembed `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，384 维，CPU / ONNX；模型不可用时回落到确定性 blake2b n-gram hash 向量，仍在同一向量空间维度内完成演示。
5. **写入**：`cf_documents` 与 `cf_chunks` 在同一事务中提交；失败回滚并删除已落盘的原始文件。

编码实现：`app/ingest.py`。

## 四、单条 SQL 混合检索

### 4.1 SQL 结构

`app/retrieval.py` 的 `build_hybrid_sql()` 生成两个有序派生表，再用 `UNION ALL` 合并：

```sql
SELECT id, document_id, page_no, heading, text, channel, rank_in_channel, channel_score
FROM (
  SELECT c.id, c.document_id, c.page_no, c.heading, c.text,
         'fts' AS channel,
         ROW_NUMBER() OVER (ORDER BY MATCH(c.text) AGAINST (%s) DESC) AS rank_in_channel,
         MATCH(c.text) AGAINST (%s) AS channel_score
  FROM cf_chunks c
  WHERE 1 = 1
    AND c.collection_id = %s
    AND c.document_id IN (%s, ...)
    AND MATCH(c.text) AGAINST (%s)
  ORDER BY MATCH(c.text) AGAINST (%s) DESC
  LIMIT %s
) fts
UNION ALL
SELECT id, document_id, page_no, heading, text, channel, rank_in_channel, channel_score
FROM (
  SELECT c.id, c.document_id, c.page_no, c.heading, c.text,
         'vector' AS channel,
         ROW_NUMBER() OVER (ORDER BY l2_distance(c.embedding, %s) ASC) AS rank_in_channel,
         l2_distance(c.embedding, %s) AS channel_score
  FROM cf_chunks c
  WHERE 1 = 1
    AND c.collection_id = %s
    AND c.document_id IN (%s, ...)
  ORDER BY l2_distance(c.embedding, %s) APPROXIMATE
  LIMIT %s
) vec
```

- 集合过滤与文档过滤同时存在于两个分支；参数占位符数量由 `9 + 2 × collection + 2 × docs` 精确校验，避免拼接错位。
- `PER_CHANNEL_LIMIT = 24`，即每路最多 24 条候选；应用层合并后返回 `TOP_K = 8`。
- 向量以字面量 `[v1,v2,...]` 传入；这是 seekdb 当前 MySQL 协议下可稳定执行的写法。
- 整条语句返回的 `channel`、`rank_in_channel`、`channel_score` 是后续 RRF 与 UI 展示通道证据的依据。

### 4.2 执行计划证据

**全库查询**（真实 EXPLAIN，1,318 个 chunk）：

```text
|ID|OPERATOR                     |NAME     |EST.ROWS|EST.TIME(us)|
|0 |UNION ALL                    |         |48      |66443       |
|1 |├─SUBPLAN SCAN               |fts      |24      |28225       |
|2 |│ └─LIMIT                    |         |24      |28225       |
|3 |│   └─WINDOW FUNCTION        |         |24      |28225       |
|4 |│     └─SORT                 |         |911     |27331       |
|5 |│       └─TEXT RETRIEVAL SCAN|c(cf_ft) |911     |26930       |
|6 |└─SUBPLAN SCAN               |vec      |24      |38218       |
|7 |  └─TOP-N SORT               |         |24      |38218       |
|8 |    └─WINDOW FUNCTION        |         |1330    |38153       |
|9 |      └─SORT                 |         |1330    |36795       |
|10|        └─VECTOR INDEX SCAN  |c(cf_vec)|1330    |36148       |
```

**限定单份文档**（3M 10-K，查询 `raw materials supply chain risk`）：

```text
|ID|OPERATOR                                         |NAME           |EST.ROWS|EST.TIME(us)|
|0 |UNION ALL                                        |               |48      |28976       |
|1 |├─SUBPLAN SCAN                                   |fts            |24      |5112        |
|2 |│ └─LIMIT                                        |               |24      |5112        |
|3 |│   └─WINDOW FUNCTION                            |               |24      |5112        |
|4 |│     └─SORT                                     |               |144     |4971        |
|5 |│       └─TEXT RETRIEVAL SCAN                    |c(cf_ft)       |144     |4912        |
|6 |└─SUBPLAN SCAN                                   |vec            |24      |23865       |
|7 |  └─TOP-N SORT                                   |               |24      |23865       |
|8 |    └─WINDOW FUNCTION                            |               |830     |23821       |
|9 |      └─SORT                                     |               |830     |22973       |
|10|        └─VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)|c(cf_chunk_doc)|830     |22576       |
```

`PRE-FILTER` 说明文档条件在向量索引扫描前下推，而不是先取全库近邻再过滤。这是「限定某份资料提问」仍然准确、可用的关键。

### 4.3 应用层融合（RRF）

```text
score(d) = Σ_channel 1 / (60 + rank_channel^1.5) × title_boost(d)
```

- `rank^1.5` 抑制「两路都一般」的候选：两路都排 20 名左右的片段，不应压过单路第 1 名；
- `title_boost = 1.35`：查询中出现的英文实体 / 数字 token 命中文档标题时加权，用于区分「3M」与「Boeing」这类同域文档；
- 输出保留 `channels`、`matched_by`（`fts` / `vector` / `fts+vector`）与 `rrf_score`，前端「技术」页可核对；
- RRF 在应用层完成，是为了让 SQL 保持「两个有序候选集」的简单契约，同时让融合参数可测试、可解释。

实现见 `app/retrieval.py` 的 `_fuse()` / `_title_boost()`；测试见 `tests/test_retrieval.py`。

## 五、可核对的证据回答

`app/answer.py` 默认走摘录式回答，不需要任何模型 Key：

1. 先按文档类型聚焦：若第一条命中是 PDF，则优先只在 PDF 命中中选句，避免中文笔记「污染」英文财报问题；
2. 对每个命中的 chunk 分句，按查询词覆盖、命中排名、句长、是否含数字、是否为小写残句等打分；
3. 每条要点最多两句（首条命中）或一句（其余），最多四条；
4. 跨页重复披露用 Jaccard ≥ 0.82 的句级近似去重，`have/has` 这类微小差异不会重复展示；
5. 只保留被要点真正使用的引用，重新从 `[1]` 连续编号；
6. 若问题为中文、命中主要为英文，明确提示「保留原句并附页码，避免转述失真」。

配置 `CITEFLOW_LLM_BASE_URL` / `CITEFLOW_LLM_API_KEY` / `CITEFLOW_LLM_MODEL` 后，回答可切换为 LLM 组织表达；system prompt 只允许使用检索证据，且引用仍由检索结果生成。LLM 失败自动回落到摘录式，不阻塞回答。

引用页通过 `GET /api/documents/{id}/page/{n}` 回查，PDF 另可请求 `/image` 获得由 pypdfium2 渲染的页图。

## 六、PowerContext 记忆链路

实现见 `app/memory.py`。

### 6.1 scope 与写入

- 首次使用时通过 `POST /v1/scopes` 创建名为「CiteFlow 研读记忆」的 scope，写入 `runtime/powercontext_scope.json`，后续复用；请求带固定 `idempotency_key`，避免重复建 scope。
- 记忆分三类：`preference`（阅读偏好）、`insight`（保存的结论）、`progress`（阅读进度）。
- 写入调用 `POST /v1/memory/remember`，kind 带 `@user_id` 标签；响应中的 `entry.citation.entry_id` 与 `x-powercontext-request-id` 会进入 UI 与镜像表。
- 每次写入同时镜像到 seekdb `cf_memories`，并记录 `backend`、`pc_entry_id`、`pc_request_id`，用于展示和降级。

### 6.2 召回与注入

- 提问时调用 `POST /v1/memory/search`（`mode=fts`），命中的偏好 / 结论进入 `applied_memories`，并由回答模块拼入可选 LLM 的上下文；
- 「记忆」页可调用 `POST /v1/context/prepare`，展示真实返回的上下文内容、scope、字节数与 request id；
- 调用 `GET /health/ready` 检查在线状态；不可用时 `status()` 返回 `seekdb-fallback`，`recall()` 改用 seekdb 镜像的 LIKE 检索，UI 明示降级，不假装在线。

### 6.3 真实验证记录

| 动作 | request id | 结果 |
| --- | --- | --- |
| 写入偏好 | `a2ca86d235a4670b` | `mem_ent_c65744d884dc41198021942578c9b3e6` |
| 召回偏好 | `1d48bc4e2807e3a3` | 1 条命中，score 0.0164，`matched_by=["fts"]` |
| `context.prepare` | `1e003b8eaa8a89a1` | status ready，661 bytes，1 条记忆 |
| 回答注入 | — | `applied_memories=["分析财报时优先关注供应链、现金流与诉讼风险。"]` |

scope：`scp_1xdtxr77pssa6gy4jb5ff1tta0`。完整上下文原文与验证脚本见 `docs/verification.md`。

## 七、失败与降级

| 依赖 | 失败时的行为 | 用户可见性 |
| --- | --- | --- |
| seekdb | API 返回 503；首页健康条显示 degraded | 顶部状态条 + 错误提示 |
| 嵌入模型 | 自动使用确定性 hash 向量；检索仍可用，语义质量下降 | 状态条显示 `hash · 384d` |
| PowerContext | 记忆写入 / 召回切到 seekdb 镜像；scope 页显示 `seekdb-fallback` | 记忆页与健康条明示 |
| LLM | 回答回落为摘录式；引用不受影响 | 回答标签显示「摘录式回答」 |
| 扫描版 PDF | 无文本时返回明确的 400：需要先 OCR | 上传错误提示 |

## 八、安全与隐私

- 默认绑定 `127.0.0.1:8848`；不主动上传文档或查询到第三方；
- 上传仅允许 PDF / MD / TXT，单文件默认上限 30 MB；文件名不参与拼接路径，落盘名由 SHA-256 生成；
- LLM 为可选，密钥只从环境变量读取，不写入日志、数据库或前端；
- 样例为公开 10-K 与自写中文笔记；仓库不包含 Qoder 截图、账号 UID、API Key、Token；
- 仓库 `.gitignore` 排除 `.env`、`runtime/`、`.venv/`、页面缓存等运行产物。

## 九、已知限制

1. 扫描版 PDF 未内置 OCR；只处理可抽取文本的文档。
2. 每页内切块，跨页长段落可能被拆开；页级引用仍准确，但语义连贯性有上限。
3. 向量通道固定取 24 条近似近邻；超大规模集合需要按数据量调整 `PER_CHANNEL_LIMIT` 与索引参数。
4. 标题实体加权是轻量启发式，不做实体识别；对无英文实体的问题等价于不加权。
5. 离线摘录式回答不会「总结成一段」，这是保证引用可核对的有意取舍；LLM 模式可改善表达，但会增加外部依赖。
6. 单机、单用户默认配置；多用户隔离目前仅靠 `x-citeflow-user` / cookie，不是生产级鉴权。
7. 使用数据为开发期本机记录，尚未覆盖外部真实用户；站点与文档均已如实标注。

## 十、代码索引

| 模块 | 职责 |
| --- | --- |
| `app/config.py` | 环境变量、路径、默认值与 LLM 开关 |
| `app/db.py` | 连接、schema、文档 / chunk 写入、查询日志、统计、记忆镜像 |
| `app/ingest.py` | PDF / MD / TXT 解析、清洗、切块、去重、页图渲染 |
| `app/embeddings.py` | fastembed 多语言嵌入与 hash 降级 |
| `app/retrieval.py` | 单 SQL 混合检索、RRF、EXPLAIN、检索证据缓存 |
| `app/answer.py` | 摘录式回答、去重、引用、可选 LLM |
| `app/memory.py` | PowerContext scope / 写入 / 召回 / context.prepare / 降级 |
| `app/server.py` | HTTP API、错误映射、静态前端托管 |
| `app/web/` | 原生三栏界面、引用面板、记忆页、技术页 |
| `scripts/` | 启动、样例导入、端到端验证、截图 |
| `tests/` | 单元测试 + 可选 seekdb 集成测试 |
