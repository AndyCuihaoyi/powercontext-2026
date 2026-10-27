# Qoder 开发记录 · CiteFlow

> 项目：CiteFlow · 研读工作台（2026 OceanBase 数据库大赛 · Qoder 生态赛题）
>
> 开发环境：Qoder Desktop（Qoder CN），项目目录 `qoder/`
>
> 开发日期：2026-10-27
>
> 记录方式：按阶段整理本次 Qoder 会话中的需求拆解、技术探针、编码、调试和自测。关键环节的 Qoder 对话截图 / 录屏由账号持有人从 Qoder 会话导出，随平台提交材料一并上传；本文件给出对应阶段与可核对的代码、命令、日志位置，避免截图脱离事实。
>
> 数据原则：本文不记录任何密钥；所有性能与使用数字均来自本机真实运行日志，并标明采集范围。

## 一、项目一句话

把论文、财报和课程笔记导入本地书架，用**一条 SQL** 同时跑 IK 全文与 HNSW 向量召回，把回答中的每条结论落回可核对的原文页码；再用 PowerContext 保存阅读偏好、结论与进度，下次打开接着读。

## 二、Qoder 在本次开发中做了什么

| 阶段 | Qoder 承担的工作 | 可核对产物 |
| --- | --- | --- |
| 需求拆解 | 阅读赛题、工作区规范与已有资产；把「网站可访问 / 可运行 / Qoder 可核对 / 技术结合有效」拆成可执行清单；确定「可回查证据的研读助手」方向 | `topics/eco-03-qoder-citeflow/00-brief.md`、`01-mapping.md`、`02-plan.md` |
| 技术探针 | 在本机 seekdb 1.4 上验证 IK 全文、HNSW 向量、`ROW_NUMBER` + `UNION ALL` 单 SQL、过滤下推与 EXPLAIN 输出；验证 PowerContext 1.2 的 scope / remember / search / context.prepare；验证 fastembed 中英跨语言向量 | `topics/.../notes/`、本文第四节、`docs/architecture.md` |
| 编码 | 创建配置、数据层、解析切块、嵌入、混合检索、证据回答、记忆、HTTP API 与原生前端 | `app/`、`scripts/`、`tests/` |
| 调试 | 定位并修复引用面板不自动打开、EXPLAIN 多字节截断、PowerContext 首次配置交互、浏览器选择器失配、跨页重复句、过滤截图跑偏等问题 | 本文第五节逐条记录 |
| 验证 | 编写并执行单元测试、seekdb 集成测试、HTTP 端到端验证、Playwright 浏览器检查与截图；核对引用回查、两类执行计划、记忆请求 ID | `tests/`、`scripts/verify_demo.py`、`runtime/verify_report.json`、`site/assets/` |
| 交付 | 编写运行说明、架构说明、验证报告、静态项目网站，整理提交清单 | `README.md`、`docs/`、`site/`、`topics/.../05-submit.md` |

本次会话的主要开发工作由 Qoder 完成。人工提供的是赛题、工作区约束、运行环境和最终提交所需的账号信息（Qoder UID、Sites 发布、平台提交），没有由人工代写核心代码。

## 三、开发过程

### 阶段 0 · 读题与边界

1. 读取 `reference/docs/2026年OceanBase数据库大赛/赛题/Qoder.md`，确认：
   - 必须基于 `oceanbase/powercontext` 的 `2026-competition-ecosystem` 分支开发，资产放入 `qoder/`；
   - seekdb 与 PowerContext 至少用其一，且必须实际参与功能；
   - 网站必须通过 Qoder Sites 发布，并设为「任何人都可访问」；
   - 提交网站 URL、代码仓库、运行说明、验证样例、Qoder 开发记录与 UID。
2. 读取工作区规范，遵守：`reference/`、`competition/` 只读；一次只推进一题；密钥只放 `.secrets/` 或环境变量；不伪造使用数据。
3. 盘点环境：Python 3.12.3、seekdb 1.4.0、PowerContext 1.2.0、Playwright 1.55 / Chromium 1187 均可用；PowerContext 因 8848 端口被应用占用，改用独立端口 17429。

**方向决策**：不做泛化的「文档聊天」，而是做研读闭环——**检索 → 回原文页 → 记住偏好与进度**。这样 seekdb 和 PowerContext 各自承担不可替代的职责，也能用真实日志证明技术确实发挥作用。

### 阶段 1 · 技术探针

在写业务代码之前，先在 seekdb 1.4 上验证关键语法，避免把假设写进架构：

| 探针 | 结果 |
| --- | --- |
| `FULLTEXT INDEX ... WITH PARSER ik` + `MATCH ... AGAINST` | 通过，中英文均可召回并返回相关度 |
| `VECTOR(384)` + `VECTOR INDEX ... (DISTANCE=l2, TYPE=hnsw, LIB=vsag, SYNC_MODE=IMMEDIATE)` | 通过，插入后可立即检索 |
| 单 statement 内两个派生表 + `ROW_NUMBER()` + `UNION ALL` | 通过，返回两路有序候选 |
| `ORDER BY l2_distance(...) APPROXIMATE` | 通过，EXPLAIN 出现 `VECTOR INDEX SCAN` |
| 文档过滤下推 | 通过，EXPLAIN 出现 `VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)` |
| PowerContext 基础记忆（SQLite、无模型 Key） | 通过，`scope` / `remember` / `search` / `context.prepare` 均返回 request id |
| fastembed 多语言模型 | 通过，384 维；实测中英相关句相似度 0.752，弱相关句 0.118 |
| 模型不可用时的降级 | 设计为确定性 blake2b n-gram hash 向量，维度仍为 384，UI 标注 `hash` |

**得到的技术契约**：

- 全文索引与向量索引在同一张 `cf_chunks` 表上；
- 两路都在 SQL 内完成排序和截断，应用层只做 RRF，不在 Python 里重排全部候选；
- 过滤条件必须能下推，否则「限定某份文档提问」会退化为全库向量扫描；
- 回答必须带引用，引用必须能通过 HTTP 回到原文页。

### 阶段 2 · 后端实现

按依赖顺序落地：

1. `app/config.py`：环境变量集中管理；默认只监听 `127.0.0.1`；LLM 三项齐全才启用。
2. `app/db.py`：建库建表；`cf_documents` / `cf_chunks` / `cf_queries` / `cf_memories` / `cf_progress`；查询日志与 p50 / p95 统计；PowerContext 不可用时的记忆镜像。
3. `app/embeddings.py`：fastembed 懒加载、批量嵌入、hash 降级；模型首次下载允许配置 HuggingFace 镜像。
4. `app/ingest.py`：PDF / Markdown / TXT 解析；PDF 按页保留 `page_no`，Markdown 按标题分节并映射为 `§n`；约 900 字切块、120 字重叠；SHA-256 去重；批量入库。
5. `app/retrieval.py`：拼装单条混合 SQL；集合 / 文档过滤；RRF（k=60，`rank^1.5`，标题实体 1.35 加权）；记录 SQL 参数、通道排名、耗时；EXPLAIN 读取与容错。
6. `app/answer.py`：默认摘录式回答，只从命中片段选句；同文档类型聚焦；近似重复句抑制；引用裁剪后重新编号；可选 OpenAI 兼容 LLM，system prompt 禁止补充外部事实。
7. `app/memory.py`：PowerContext scope 自动创建与持久化；偏好 / 结论 / 进度写入；FTS 召回；`context.prepare`；每次写入同步镜像到 seekdb，服务不可用时自动降级并暴露 `backend`。
8. `app/server.py`：健康检查、书架、上传、删除、原文页 / 页图、检索、回答、EXPLAIN、统计、记忆、进度等 API；统一错误映射；静态前端托管。

**数据模型要点**：`cf_chunks` 同时挂 IK 全文索引与 HNSW 向量索引；`cf_queries` 为每次检索留下真实日志；`cf_memories` 不是第二套记忆产品，只是 PowerContext 的可见镜像与降级路径。

### 阶段 3 · 前端实现

前端使用原生 HTML / CSS / JS，不引入构建步骤，避免评审环境额外安装 Node：

- 三栏工作台：资料库 / 提问与回答 / 证据·记忆·技术；
- 支持集合、单文档勾选过滤；快捷问题；语言选择；`⌘/Ctrl + Enter`；
- 回答中的 `[n]` 与右侧证据页联动；PDF 可在「原文 / 页图」间切换，页图由 pypdfium2 在本地渲染；
- 记忆页展示 PowerContext 状态、scope、记忆列表、新增记忆、上下文预览；
- 技术页展示当次 SQL、EXPLAIN、通道计数、延迟与使用统计；
- PowerContext / 嵌入 / LLM 的降级状态在界面上明示，不静默失败。

### 阶段 4 · 调试与修复

以下问题都在本次会话中真实出现并修复，保留记录以便复核 Qoder 的调试过程：

| # | 现象 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | `/api/ask` 后右侧证据面板不自动打开 | 前端把整个 citation 对象传给了只接受索引的 `openCitation()` | 改为 `openCitation(payload.answer.citations[0].index)`；无引用时清空面板 |
| 2 | `/api/retrieval/explain` 返回 400：`'utf-8' codec can't decode bytes in position 199-200` | seekdb 在固定 200 字节处截断 EXPLAIN 中的长中文字面量，正好切断一个 UTF-8 字符 | EXPLAIN 连接改用 latin-1 读取字节；写回时再用 latin-1 → UTF-8 修复；避免连接层二次编码（先将 statement 编码为 bytes） |
| 3 | `scripts/run_powercontext.sh` 首次生成配置时挂起 | `powercontext config init --template` 仍提示 `Write ...? [Y/n]`，非交互环境读到 EOF 退出 | 用 `printf 'y\n'` 通过管道显式确认；脚本等待 `/health/ready` 后再返回 |
| 4 | Playwright 找不到 `.answer-bullets li`、`#tab-tech` | 选择器基于猜测，实际 DOM 为 `.answer-bullet`，技术页按钮是 `.tabs .tab[data-tab='tech']` | 先读取 DOM 结构再写选择器；截图脚本加入显式等待 |
| 5 | 回答出现近似重复的两条「俄罗斯供应中断」 | 10-K 在不同页重复披露，措辞仅 `have/has` 差异 | 对候选句做 Jaccard 近似去重（阈值 0.82），保留首次出现 |
| 6 | 集成测试断言 `TEXT RETRIEVAL SCAN` 偶发失败 | 只有一条 probe 文档的临时集合会被优化为 `TABLE RANGE SCAN`，不是功能退化 | 集成测试只断言混合结构和 `VECTOR INDEX`；真实 1,318 片段数据集上另行核对 `TEXT RETRIEVAL SCAN` |
| 7 | 过滤模式截图答案跑题到 p.64 | 截图脚本使用泛化快捷问题，命中环保披露 | 改为 `raw materials supply chain risk`，并选择 3M 文档，得到 p.6 / p.13 / p.20 与 `PRE-FILTER` 计划 |
| 8 | 移动端截图只显示书架 | 页面较长，截图从顶部开始 | 截图前把提问卡滚动到视口；人工检查 390px 宽度下无横向溢出 |
| 9 | 引用列表包含未被要点使用的页 | 初始实现把所有命中都放进 citations | 只保留 bullet 真正引用的位置，并重新连续编号 |

### 阶段 5 · 测试与验证

- 单元测试覆盖 SQL 占位符、RRF / 标题加权、引用裁剪、同类型聚焦、无证据路径、语言选择、Markdown 解析、切块、记忆类型与截断；
- seekdb 集成测试使用临时集合导入 probe 文档，验证入库、双通道召回、EXPLAIN 和清理；
- 结果：默认 **13 passed, 1 skipped**；开启 `CITEFLOW_TEST_SEEKDB=1` 后 **14 passed**；
- HTTP 端到端验证 **24/24 检查通过**：4 个代表性问题、1 个文档过滤计划、引用页 HTTP 200、PowerContext 写入 / 召回 / context.prepare / 回答注入、统计日志；
- 浏览器自测：回答自动关联引用、证据页可切页图、技术页可刷新 EXPLAIN、记忆页展示在线状态，控制台无错误；截图 6 张桌面 + 1 张移动端。

完整数字、request id 和命令见 [`docs/verification.md`](docs/verification.md)，原始报告在 `runtime/verify_report.json`（运行时生成，不提交）。

### 阶段 6 · 交付整理

- `README.md`：项目说明、技术亮点、快速开始、配置、测试、数据来源；
- `docs/architecture.md`：组件、DDL、单 SQL、执行计划、RRF、PowerContext、降级与限制；
- `docs/verification.md`：环境、步骤、预期结果、真实验证记录与截图索引；
- `site/`：Qoder Sites 静态站，含项目简介、功能、演示截图、仓库链接、运行说明、真实使用数据与开发记录；
- `topics/eco-03-qoder-citeflow/`：实现日志、测试报告、提交清单。

## 四、技术证据索引

| 证据 | 位置 |
| --- | --- |
| 单条混合 SQL 的拼装与参数校验 | `app/retrieval.py` 的 `build_hybrid_sql()` / `search()` |
| 全文 + 向量索引 DDL | `app/db.py` 的 `init_schema()` |
| 文档过滤下推计划 | `docs/verification.md`「执行计划」 |
| PowerContext 真实请求 ID | `docs/verification.md`「记忆链路」 |
| 查询日志与 p50/p95 | `app/db.py` 的 `usage_stats()`；运行时 `GET /api/stats` |
| 回答引用裁剪 | `app/answer.py` 的 `_extractive_answer()` |
| 降级路径 | `app/memory.py` 的 `status()` / `recall()`；`app/embeddings.py` |
| 真实浏览器画面 | `site/assets/*.png` |

## 五、可复现命令

```bash
cd qoder
scripts/run.sh
# 另开终端
.venv/bin/python scripts/seed_demo.py
.venv/bin/python scripts/verify_demo.py
CITEFLOW_TEST_SEEKDB=1 .venv/bin/python -m pytest
```

## 六、边界与未完成事项

- 使用数据来自开发期本机验证（2026-10-27），**不是外部用户数据**；站点已明确标注，没有购买流量或伪造反馈。
- 离线回答是摘录式，不是生成式综述；配置 LLM 后可切换，但 LLM 不参与检索与引用生成。
- 扫描版 PDF 需要先 OCR，本项目未内置 OCR。
- Qoder Sites 发布、Qoder 账号 UID、平台提测需要账号持有人完成；相关步骤见 `topics/eco-03-qoder-citeflow/05-submit.md`。
