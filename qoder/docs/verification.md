# CiteFlow 验证样例与自测记录

> 验证时间：2026-10-27 12:11（UTC+8）
>
> 环境：Ubuntu / Linux x64 · Python 3.12.3 · seekdb 1.4.0.0 · PowerContext 1.2.0 · fastembed 0.9.1（384 维）· Playwright Chromium 1187
>
> 验证入口：`scripts/verify_demo.py`（HTTP 端到端）· `pytest`（单元 + 可选集成）· `scripts/capture_screenshots.py`（浏览器）
>
> 原始结果：`runtime/verify_report.json`（运行时生成，已被 `.gitignore` 排除；本文保留其中的关键记录）

## 一、验证目标

对应赛题 3.1 的四项基础验收：

| 验收点 | 本项目如何证明 |
| --- | --- |
| 项目能够运行 | 一键启动后 `/api/health` 返回 `ok`；样例导入成功；页面可打开 |
| 主要功能可体验 | 一次提问返回双通道命中、带页码回答、可回查原文页；记忆页可见保存内容 |
| seekdb 实际作用 | 单条 SQL 同时触发 `TEXT RETRIEVAL SCAN` 与 `VECTOR INDEX SCAN`；文档过滤触发 `VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)`；查询日志可统计 |
| PowerContext 实际作用 | 偏好写入后返回 entry id / request id；新查询召回该偏好；`context.prepare` 返回可注入上下文；回答标记已应用 |

## 二、初始数据

`data/samples/` 共 5 份文件：

| 文件 | 类型 | 来源 | 页数 | 片段数 |
| --- | --- | --- | --- | --- |
| `3M_2022_10K.pdf` | PDF | SEC EDGAR / FinanceBench 公开 10-K | 252 | 830 |
| `BOEING_2022_10K.pdf` | PDF | SEC EDGAR / FinanceBench 公开 10-K | 190 | 471 |
| `powercontext_memory.md` | Markdown | 本项目撰写 | 7（节） | 6 |
| `research_reading_method.md` | Markdown | 本项目撰写 | 7（节） | 6 |
| `seekdb_hybrid_notes.md` | Markdown | 本项目撰写 | 6（节） | 5 |
| **合计** |  |  | **462** | **1,318** |

字符数合计 1,438,159。SHA-256 去重：重复导入同一批文件会返回 `duplicate: true`，不会重复写入。

## 三、从零启动

```bash
cd qoder
cp .env.example .env        # 可选；默认值即可在本机运行
scripts/run.sh              # 创建 .venv、启动 seekdb、可选启动 PowerContext、启动 CiteFlow
```

预期：

```text
seekdb 已在 127.0.0.1:2891 就绪
PowerContext 已在 127.0.0.1:17429 就绪（若安装）
CiteFlow 已启动: http://127.0.0.1:8848
```

健康检查：

```bash
curl -s http://127.0.0.1:8848/api/health | python3 -m json.tool
```

本次实测关键字段：

```json
{
  "status": "ok",
  "seekdb": {"ok": true, "version": "5.7.25-OceanBase seekdb-v1.4.0.0"},
  "embedding": {
    "backend": "fastembed",
    "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "dim": 384,
    "status": "ready"
  },
  "memory": {
    "mode": "powercontext",
    "available": true,
    "scope_id": "scp_1xdtxr77pssa6gy4jb5ff1tta0"
  },
  "llm": {"available": false},
  "counts": {"documents": 5, "chunks": 1318, "pages": 462}
}
```

> 未配置 LLM 时 `llm.available=false` 是预期行为，回答走摘录式，不影响验收。

## 四、导入与端到端验证

```bash
.venv/bin/python scripts/seed_demo.py
.venv/bin/python scripts/verify_demo.py
```

`verify_demo.py` 会依次执行：

1. 健康检查；
2. 导入样例（重复文件自动跳过）；
3. 读取书架与集合；
4. 4 个代表性问题：

   | 标签 | 问题 | 考察点 |
   | --- | --- | --- |
   | 3M 财报 | 3M 在 2022 年面临的主要供应链和原材料风险是什么？ | 中文问英文财报；全文 + 向量；页级引用 |
   | Boeing 财报 | 波音 2022 年的商用飞机交付与订单情况如何？ | 同域文档区分；数字与表格页 |
   | 中文方法笔记 | 做财报研读时应该怎样记录证据？ | 中文 IK 全文；Markdown 小节引用 `§n` |
   | 英文跨语言 | What are the main supply chain risks? | 英文查询跨文档；双通道命中 |

5. 对每个问题检查：返回条数、双通道计数、引用数量、回答非空；
6. 请求每条引用的 `/api/documents/{id}/page/{n}`，确认 HTTP 200；
7. 读取最近一次检索的 EXPLAIN，检查全文与向量两类扫描；
8. 选择 3M 文档，以 `raw materials supply chain risk` 再做一次过滤检索，检查 `PRE-FILTER`；
9. 记忆链路：写入偏好 → FTS 召回 → `context.prepare` → 回答中 `applied_memories`；
10. 读取 `/api/stats`，确认查询次数与延迟分位可核对。

退出码非 0 表示任一检查失败；结果同时写入 `runtime/verify_report.json`。

### 4.1 检查结果

**24/24 通过**，`ok: true`。关键检查：

| # | 检查 | 结果 |
| --- | --- | --- |
| 1 | seekdb 可连接 | `5.7.25-OceanBase seekdb-v1.4.0.0` |
| 2 | 嵌入后端可用 | `fastembed · 384d · ready` |
| 3 | 样例导入 | 5 个文件，116 ms（重复导入） |
| 4 | 书架非空 | 5 文档 / 1,318 片段 / 462 页 / 1,438,159 字符 |
| 5–8 | 3M：证据、双通道、引用回查、EXPLAIN | 8 hits · 3 citations · fts=24 / vector=24 · p.20 HTTP 200 · 两类扫描均为 True |
| 9–11 | Boeing：证据、双通道、引用回查 | 8 hits · 3 citations · fts=24 / vector=24 · p.35 HTTP 200 |
| 12–14 | 中文方法笔记：证据、双通道、引用回查 | 8 hits · 3 citations · fts=10 / vector=24 · §5 HTTP 200 |
| 15–17 | 英文跨语言：证据、双通道、引用回查 | 8 hits · 3 citations · fts=24 / vector=24 · p.6 HTTP 200 |
| 18 | 结构化过滤下推 | 3M 文档 · `TEXT RETRIEVAL SCAN=True` · `PRE-FILTER=True` |
| 19 | PowerContext 在线 | `powercontext · database ready` |
| 20 | 记忆写入 PowerContext | entry `mem_ent_c65744d884dc41198021942578c9b3e6` · request `a2ca86d235a4670b` |
| 21 | 记忆检索可复用 | 1 hit · request `1d48bc4e2807e3a3` |
| 22 | `context.prepare` 可注入 | 661 bytes · request `1e003b8eaa8a89a1` |
| 23 | 记忆进入回答上下文 | `applied=1` |
| 24 | 查询日志可核对 | 42 次查询 · p50 86.0 ms · p95 1403.0 ms |

### 4.2 回答与引用示例

**问题**：3M 在 2022 年面临的主要供应链和原材料风险是什么？

**实际回答**（摘录式，保留英文原句）：

```text
根据已导入资料中的检索证据：
- 3M also has other operations that source certain raw materials from suppliers in Russia and have experienced related supply disruption due to the conflict. [1]
- Though 3M monitors relevant factors as well as options to mitigate potential impacts, it is not able to predict the extent to which these circumstances may have a material effect on 3M’s consolidated results of operations or financial condition. [1]
- Raw Materials In 2022, many geopolitical, logistics, and disruptive events caused imbalance in the global supply chain, similar to the past few years. [2]
- * Unexpected events, such as those related to the coronavirus (COVID-19) public health crisis, may increase the Company's cost of doing business and disrupt the Company's operations. [3]

检索到的主要是英文原文，以下保留原句并附页码，避免转述失真。
```

**引用**：

| 编号 | 文档 | 位置 | 通道 | RRF |
| --- | --- | --- | --- | --- |
| [1] | 3M 2022 10-K（年报） | p.20 | vector | 0.022131 |
| [2] | 3M 2022 10-K（年报） | p.6 | vector | 0.021487 |
| [3] | 3M 2022 10-K（年报） | p.10 | vector | 0.020707 |

**耗时**：嵌入 5 ms · SQL 查询 53 ms · 总计 87 ms（含融合、引用与日志）。

**中文笔记示例**：

```text
根据已导入资料中的检索证据：
- 证据记录模板 每条笔记保留四个字段： 1. 结论：一句话，可被反驳； 2. 原文：直接引用，保留限定词与时间范围； 3. 位置：文档标题 + 页码或章节； 4. 通道：全文命中、向量命中，或两者都有。 [1]
- CiteFlow 的引用标签采用 `p.42`（PDF 页码）或 `§3`（Markdown 小节），复制到笔记里仍能回查。 [1]
- 若三者不一致，优先相信经过审计的报表数字，并把差异本身记成一条待办。 不要用单一片段下结论。 检索系统返回的是“相关”，不是“充分”。 [2]
- 例如“供应链怎么样”太宽，改成“2022 年原材料与物流成本上升对毛利率的影响”。 [3]
```

引用分别命中《财报研读：从问题到证据的三遍法》§5 / §4 / §3；三条均为 `fts+vector`。

**英文跨语言示例**：查询 `What are the main supply chain risks?` 命中 3M p.6、Boeing p.16、3M p.10，三条均为 `fts+vector`，说明中文问题 / 英文问题都能落在同一语料上。

### 4.3 执行计划

全库查询的 EXPLAIN 包含：

```text
|5 |│       └─TEXT RETRIEVAL SCAN|c(cf_ft) |
|10|        └─VECTOR INDEX SCAN  |c(cf_vec)|
```

限定 3M 文档后：

```text
|5 |│       └─TEXT RETRIEVAL SCAN                    |c(cf_ft)       |
|10|        └─VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)|c(cf_chunk_doc)|
```

完整计划与 SQL 见 `docs/architecture.md` 第四节。UI 的「技术」页可以现场刷新并查看原始计划。

### 4.4 记忆链路原文

写入偏好：

```json
{
  "record": {
    "kind": "preference",
    "text": "分析财报时优先关注供应链、现金流与诉讼风险。",
    "backend": "powercontext",
    "entry_id": "mem_ent_c65744d884dc41198021942578c9b3e6",
    "request_id": "a2ca86d235a4670b"
  }
}
```

召回：

```json
{
  "backend": "powercontext",
  "request_id": "1d48bc4e2807e3a3",
  "hits": [{
    "text": "分析财报时优先关注供应链、现金流与诉讼风险。",
    "kind": "preference",
    "score": 0.0164,
    "matched_by": ["fts"]
  }]
}
```

`context.prepare` 返回 `status=ready`、`content_bytes=661`、request `1e003b8eaa8a89a1`。内容包含：

```text
BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1
{"trust":"untrusted_history","items":[{"citation":{"memory_ref":{...},"entry_id":"mem_ent_c65744d884dc41198021942578c9b3e6",...},"content":"分析财报时优先关注供应链、现金流与诉讼风险。","truncated":false}]}
END_POWERCONTEXT_PREPARED_CONTEXT_V1
```

本次查询的 `applied_memories` 为 `["分析财报时优先关注供应链、现金流与诉讼风险。"]`。

## 五、单元与集成测试

```bash
.venv/bin/python -m pytest
CITEFLOW_TEST_SEEKDB=1 .venv/bin/python -m pytest
```

本次实测：

```text
# 默认（不依赖 seekdb）
13 passed, 1 skipped in 0.11s

# 开启 seekdb 集成
14 passed in 0.52s
```

覆盖内容：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_retrieval.py` | SQL 占位符数量（无过滤 / 集合 / 集合 + 文档）、RRF 融合、标题加权 |
| `tests/test_answer.py` | 引用裁剪与重编号、同文档类型聚焦、无证据响应、语言选择、近似重复句 |
| `tests/test_ingest.py` | Markdown 分节、文本清洗、长段切块、标题推断 |
| `tests/test_memory.py` | 记忆类型规范化、标签、超长截断 |
| `tests/test_integration.py` | 真实 seekdb 入库、双通道召回、EXPLAIN；使用临时集合并清理 |

> 集成测试使用单一 probe 文档时，优化器可能选择 `TABLE RANGE SCAN` 而非 `TEXT RETRIEVAL SCAN`；测试因此断言「存在向量索引扫描 + 全文 MATCH 结构」，真实 1,318 片段数据集上的计划由端到端脚本另行验证。

## 六、浏览器与截图

```bash
.venv/bin/python -m playwright install chromium
.venv/bin/python scripts/capture_screenshots.py
```

脚本会打开真实页面、导入样例、执行一次提问、打开引用与页图、刷新 EXPLAIN，并保存：

| 文件 | 内容 |
| --- | --- |
| `site/assets/home.png` | 初始工作台、书架、状态条 |
| `site/assets/answer.png` | 3M 提问后的回答与引用面板（自动打开 p.20） |
| `site/assets/evidence.png` | PDF 原文页图模式，核对版式 |
| `site/assets/memory.png` | PowerContext 在线状态与记忆列表 |
| `site/assets/tech.png` | 当次 SQL、EXPLAIN、使用统计 |
| `site/assets/tech-filtered.png` | 限定 3M 文档 + `PRE-FILTER` 计划 |
| `site/assets/mobile.png` | 390 px 宽度下的移动布局 |

浏览器检查结果：引用自动关联、页图可切换、技术页可刷新、记忆页状态正确、控制台无报错。截图由真实运行页面生成，未做内容拼接。

## 七、使用统计快照

`/api/stats` 在验证脚本结束时的真实值：

| 指标 | 值 |
| --- | --- |
| 文档数 | 5 |
| 片段数 | 1,318 |
| 页数 | 462 |
| 字符数 | 1,438,159 |
| 查询次数 | 42 |
| p50 延迟 | 86.0 ms |
| p95 延迟 | 1403.0 ms |
| 平均延迟 | 376.9 ms |
| 回答模式 | 摘录式 31 · 仅检索 11 |
| 采集日期 | 2026-10-27（UTC+8） |

说明：

- 这些是本机开发期验证数据，**不是外部用户数据**；没有伪造访问量、用户评价或提效对比。
- p95 包含首次预热 / 页面渲染等长尾请求，故明显高于 p50。
- 截图与人工操作继续产生查询，站点展示使用 `scripts/export_site_data.py` 从 `/api/stats` 导出的最新快照；数值可能略高于上表，但来源一致。
- 后续真实用户数据应在获得授权、去除个人信息后补充到同一格式。

## 八、已知限制与人工核对项

- Qoder Sites 发布与无痕访问需要账号持有人执行，无法由本地脚本代替；
- Qoder 账号 UID 与对话截图属于提交材料，不写入公开仓库；
- 样例 PDF 为公开财报，若评审使用其他文件，首次嵌入耗时取决于文档大小与是否已下载模型；
- 没有模型 Key 时回答为摘录式，这是设计取舍，不是故障；
- 清理：删除测试集合或文档不会影响其他集合；集成测试会自行清理临时文档。
