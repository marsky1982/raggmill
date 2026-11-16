# raggmill 功能地图（Feature Map）

> **文档状态**：2026-11，基于当前代码静态阅读整理。
> **口径**：接口名与文件路径比行号稳定；本文只描述「能做什么、入口在哪、落盘到哪」，不复制实现细节。
> 逐页能力、接口清单、横切机制、已知边界都在这里；架构层面的说明见 [../docs/design/architecture.md](../docs/design/architecture.md)。

---

## 0 总览

### 0.1 产品定位

本地优先（single-machine / single-user / 无数据库 / 全部落 `data/`）的 Web 工作台，把 ragas 的合成测试集生成能力可视化：

```
文档 → [Step 1] 知识图谱(KG) → [Step 2] 测试集 → 导出 CSV / JSONL
                                        ↘ [Step 3] 质量评估（可选）
```

**ragas 是引擎、本工具是上层工作台**：只做 ragas SDK 的消费方，不改 ragas 源码；所有 ragas 调用集中在 `backend/app/ragas_adapter/`，路由层不直接 import ragas。

技术形态：后端 FastAPI（Python 3.13），前端 React 19 + TypeScript + Tailwind v4 + shadcn/ui（Vite）；前后端由 `run.py` / `start.ps1` 统一拉起，前端用 Vite 代理 `/api` 到实际后端端口。

### 0.2 五条数据加工路径

| # | 路径 | 入口 | 产物 |
|---|---|---|---|
| 1 | 本地上传文档 → 建图 | 文档页「上传文档」→ KG 页「生成知识图谱」 | 本地 KG |
| 2 | RAGFlow 连接 → 同步 → 投影 | KG 页 RAGFlow 面板「同步到工作台」 | 投影 KG（`mode="ragflow-projection"`） |
| 3 | 外部整份 KG JSON 导入 | 后端 `POST /api/testset/generate-external`（`mode=kg_json`） | 直接出题，不落 KG 列表 |
| 4 | external-chunks（散装分块） | 同上，`mode=chunks` | 直接出题 |
| 5 | external documents（含正文的文档节点） | 同上，`mode=documents` | 直接出题 |

> 路径 3/4/5 的后端能力完整，但前端目前只做本工作台 KG 的下拉选择，UI 上没有外部导入入口。

### 0.3 页面导航

`App.tsx` 用 `useState` 切换，无路由库：

| 步骤 | 页面组件 | 用户看到的名字 | 主要职责 |
|---|---|---|---|
| 1 | `src/pages/ConfigPage.tsx` | 模型配置 | 三类预设（LLM / LLM(测试集) / Embedding）+ RAGFlow 连接 |
| 2 | `src/pages/DocumentsPage.tsx` | 文档 | 上传 / 选择文件夹 / 删除 / token 估算 |
| 3 | `src/pages/KgPage.tsx` | 知识图谱 | 建图 / 断点继续 / 图谱列表 / RAGFlow 同步 |
| 4 | `src/pages/TestsetPage.tsx` | 测试集 | 能力检查 / 成本预估 / 生成 / 补生成 / 质检 / 导出 |
| 5 | `src/pages/EvaluationPage.tsx` | 质量评估 | 导入用例 JSON → 跑五指标评测 |

侧栏底部另有主题切换按钮；`DebugDrawer` 是全局调试抽屉。

### 0.4 落盘布局（`data/`）

| 路径 | 内容 |
|---|---|
| `data/secret.key` | Fernet 密钥（api_key 加密） |
| `data/.backend.lock` | 单实例锁（写 PID） |
| `data/presets/*.json` | 三类预设（api_key 密文） |
| `data/documents/index.json` + `data/documents/uploads/` | 文档元数据与原文 |
| `data/kg/{kg_id}.json` | 图谱快照 |
| `data/kg/checkpoints/{run_id}.meta.json` + `{run_id}.json` | 建图断点（meta + KG 快照） |
| `data/testset/{ts_id}.json` | 测试集（含 `usage` token 账单） |
| `data/ragflow/connections/{id}.json` | RAGFlow 连接（api_key 加密） |
| `data/ragflow/sources/{source_id}/snapshots/` + `current.json` | 不可变快照 + 当前指针 |
| `data/evaluations/` | 评测结果 |

`DATA_DIR` 可用环境变量 `RAGAS_STUDIO_DATA_DIR` 覆盖（测试与 e2e 都用它指向临时目录，不读仓库 `data/`）。

---

## 1 配置页（`ConfigPage.tsx`）

### 1.1 三类预设

| tab 文案 | kind 值 | 用途 |
|---|---|---|
| LLM（知识图谱） | `llm` | Step 1 建图：摘要、主题、实体等 transform |
| LLM（测试集生成） | `llm_testset` | Step 2 出题（人格 / 场景 / 样本）；未配置时回退到 `llm` |
| Embedding（向量化） | `embedding` | 摘要向量、相似度边 |

- 列表来自 `GET /api/presets`，前端按 `kind` 分组。
- 回退规则：测试集页优先取 `llm_testset` 的默认项，再取任意 `llm_testset`，再取 `llm` 默认项，最后取任意 `llm`；Embedding 同理。
- `storage.PRESET_KINDS` 是合法 kind 的唯一来源。

### 1.2 预设的增删改查

| 操作 | 接口 |
|---|---|
| 列表 | `GET /api/presets` |
| 新增 | `POST /api/presets`（`kind` 必填；同名同 kind → 409） |
| 编辑 | `PUT /api/presets/{id}` |
| 删除 | `DELETE /api/presets/{id}` |
| 设默认 | `POST /api/presets/{id}/set-default?kind=` |
| 连通自检 | `POST /api/presets/{id}/test` |

- 必填校验：名称、模型；`api_key` 里填了 URL 会被拦截；同名同 kind 前端先拦一次、后端 409 兜底。
- 保存时基于旧 `extra` 合并，保留表单之外的项（如 `streaming`）。
- 同 kind 的第一个预设自动成为默认；更新 api_key 不会重置默认状态。

### 1.3 temperature / 推理强度 / 思考模式

| 控件 | 字段 | 落点 | 边界 |
|---|---|---|---|
| 温度 | `temperature` | `extra.temperature` | 留空 = 不传 |
| 推理强度 | `reasoning_effort` | `extra.reasoning_effort` | 可选值唯一定义在 `storage.REASONING_EFFORTS` |
| 思考模式 | `enable_thinking` | `extra.enable_thinking` | **仅 DashScope 兼容端点可设**，其余端点置灰且不下发 |

### 1.4 RAGFlow 连接

| 操作 | 接口 |
|---|---|
| 列表 / 新增 | `GET` / `POST /api/ragflow/connections` |
| 编辑 / 删除 | `PATCH` / `DELETE /api/ragflow/connections/{id}`（只删凭据，已同步图谱与测试集保留） |
| 测试连接 | `POST /api/ragflow/connections/{id}/test` |

- API key 走 Fernet 加密存 `api_key_enc`，接口**只回 `api_key_mask`**。
- `legacy=true` 的旧布局连接地址只读；改地址必须同时给新 key（否则 400）。
- verdict 六枚举：`ready` 可用 / `partial` 部分可用 / `graph_empty` 图谱为空 / `auth_failed` 鉴权失败 / `incompatible` 接口不兼容 / `unreachable` 连不上。
- `/test` 只判「连接是否可用」，**不代表能出题**；出题前要在 KG 页对具体知识库「检查图谱」。

### 1.5 边界与失败表现

- 无默认 LLM 时，测试集页生成直接报「未找到默认 LLM / Embedding 预设，请先到「模型配置」创建并设为默认」。
- 保存失败在对应输入框下方就地渲染错误，不只记调试日志；加载失败进调试抽屉。

---

## 2 文档页（`DocumentsPage.tsx`）

### 2.1 上传文档

- 接口：`POST /api/documents/upload`（multipart，默认 `overwrite=false`）。
- 允许扩展名：`.md / .html / .docx / .txt`（前后端同一份白名单）。
- 安全：路径穿越校验、`check_id`、文件名取 basename。

### 2.2 选择文件夹

- 前端走 `POST /api/documents/upload` 逐个上传。
- 后端另有 `POST /api/documents/folder`（读服务器本地目录批量登记），前端未使用。

### 2.3 文档 ID 稳定性

- `stable_doc_id(name) = "doc-" + md5(文件名)[:8]`，跨重启不漂移。
- 文档身份四字段（`doc_id / source_ref / source_type / doc_name`）唯一构造处是 `backend/app/ragas_adapter/doc_identity.py`，别处不得自行拼接。

### 2.4 删除（软删三件套）

- 接口：`DELETE /api/documents/{doc_id}`。
- 语义：**先试物理 `unlink`**；被拦截（OSError，沙箱环境）则降级软删——JSON 写 `_deleted: true`，uploads 记 `.trashed.json` 墓碑；**所有读路径过滤 `_deleted`**；启动时 `_purge_soft_deleted()` 清理历史残留。
- 没有「恢复已删除」功能。

### 2.5 token 估算

- 接口：`POST /api/documents/estimate`。
- 前端分桶展示：<100 基础 / 101–500 中等 / >500 长上下文。

---

## 3 知识图谱页（`KgPage.tsx`）

### 3.1 生成知识图谱（Step 1）

接口：`POST /api/kg/generate`（`GenIn` 继承 `GenerationPolicy`）。

| 参数 | 后端默认 | 前端是否可填 |
|---|---|---|
| `document_ids` | 必填（续跑时忽略） | 是（多选） |
| `llm_preset_id` / `embedding_preset_id` | 必填 | 是 |
| `name` | 空（自动命名） | 是 |
| `run_id` / `resume` | 空 / false | 是（断点卡片） |
| `min_tokens` / `max_tokens` | 500 / 1000 | 是 |
| `enhance_chunks` | true | 否（恒开） |
| `token_budget` | 0（不限） | 否（前端不发送） |
| `max_retries` | 3 | 否 |
| `llm_timeout` | 180 | 否 |
| `max_workers` | 8 | 否 |

> 后四个字段后端支持且有取值范围（1..10 / 1..1800 / 1..32），但目前只能通过 API 传。

**执行结构**：`ragas_adapter/kg.py` 用 `StagedJob` 阶段化，每个 transform 一个 `Stage`。

**完成出口**：

- `completed` → 染色（按 `child` 边写 `doc_id` 等身份字段）+ 写 KG JSON + 更新索引 + 删断点。
- `failed` / `budget_exceeded` → 保留断点；任务 `stop_reason=budget_exceeded`（状态仍是 failed）。
- `canceled` → 不落图（断点也不写）。

### 3.2 断点续跑

- 断点文件：`data/kg/checkpoints/{run_id}.meta.json` + `{run_id}.json`。
- 前端页面顶部显示「发现可恢复的生成断点」卡片，提供「从断点继续」/「放弃」（`DELETE /api/kg/checkpoints/{run_id}`）。
- **续跑以断点 meta 里的 `doc_ids` 为准**，忽略前端传的 `document_ids`。
- 续跑签名排除四项策略参数（预算 / 重试 / 超时 / 并发）——改这四项不使断点失效。

### 3.3 图谱列表 / 预览 / 删除

| 操作 | 接口 | 说明 |
|---|---|---|
| 列表 | `GET /api/kg` | 卡片徽章「演示」来自 `meta.mode === "demo"` |
| 详情 | `GET /api/kg/{kg_id}` | |
| 预览 | `GET /api/kg/{kg_id}/preview?max_nodes=150` | 力导向 SVG 自绘；节点色 `document #5E6AD2` / `chunk #3D9A6B` |
| 删除 | `DELETE /api/kg/{kg_id}?detach_dependents=` | 带依赖处置（是否解绑引用它的测试集） |

### 3.4 RAGFlow 面板

| 功能 | 交互 | 接口 |
|---|---|---|
| 选连接 + 选知识库 | 两个下拉 | `GET /api/ragflow/connections`、`.../{id}/datasets` |
| 检查图谱 | 只读抽样，0 token、不写快照 | `POST .../datasets/{dataset_id}/probe` |
| 同步到工作台 | 建快照 + 投影 + 出 KG | `POST .../sync` |
| 强制重新读取 | 忽略增量指纹 | 同上（带强制参数） |
| 快照查看 | — | `GET /api/ragflow/sources/{source_id}/snapshot` |
| 实体口径设置 | 消歧模式等 | `GET/PUT /api/ragflow/settings` |

- `probe` 默认抽样 3 篇、上限 10 篇，请求上限 `3 + 2×N`；verdict 六枚举同 §1.4。
- 同步是 `StagedJob` 五阶段：`fetching → entity_resolution → projection → inherit → commit`；互斥只靠任务租约。
- 消歧四模式 `disabled / rules / llm / hybrid`，前端只暴露二态（`disabled / hybrid`）；`llm` / `hybrid` 需要可用的 `llm_testset` 或 `llm` 预设，否则 `preset_unavailable`。

### 3.5 能力判定

`GET /api/testset/capabilities/{kg_id}?relation_property=` 返回三类合成器各自的 `label / available / effective_available / auto_enhanceable / reason / needs` 与 `recommended_props`。

- 不可用原因：缺摘要向量（`summary_embedding`）、缺 `entities`、缺对应边（分块级或文档级 `summary_similarity`、`entities_overlap`）。
- 投影 KG 缺的素材可在生成前自动补齐（`auto_enhanceable`），只提示不阻塞。

### 3.6 演示模式

- `RAGAS_DEMO=1` → `config.FORCE_DEMO=True`；`kg.py` 的 `_demo_kg` 产出假图谱，不消耗 token。
- KG 演示标记落在 `meta.mode = "demo"`，前端徽章「演示」。

---

## 4 测试集页（`TestsetPage.tsx`）

### 4.1 页面结构

`TestsetPage` 只做状态编排：表单在 `components/testset/GenerateForm.tsx`，样本在 `SampleList.tsx`，质检在 `HealthReportDialog.tsx`，导出在 `ExportMenu.tsx`，补生成在 `ProvenanceCard.tsx`。

### 4.2 选择知识图谱与能力检查

- 下拉显示 `名称（N 节点 · RAGFlow 投影）`。
- 选定 KG / 换连线依据时自动刷新能力判定。
- `blockedKeys`（当前分布需要但缺素材、且不能自动补齐的题型）非空时，「生成测试集」按钮禁用并提示调整比例或重建 KG。
- 未使用的题型只提示「当前分布未使用，不影响生成」。

### 4.3 生成参数

| 参数 | 默认 | 边界 |
|---|---|---|
| 测试集条数 | 10 | 1–500 |
| 测试集名称 | 空（留空按 KG 名自动命名） | 长度与非法字符前端校验 |
| 使用默认分布 | 勾选（0.5 / 0.25 / 0.25） | 取消后可拖三条比例滑杆；和 ≠ 1 时出现「点击归一化」 |
| 多跳连线依据 | `summary_similarity` | 可选 `summary_similarity` / `entities_overlap_score`；质检报告给推荐值 |
| 出题语言 | `zh` | 选项来自 `GET /api/testset/languages` |
| 失败重试上限 | 3 | 1–10；每次重试会重发整份 prompt |
| 单次超时（秒） | 180 | 前端 30–600；后端允许 1–1800 |
| 并发数 | 8 | 1–32 |
| token 预算 | 0（不限） | 超限自动中止 |
| 人格数 | 3 | 1–10 |

条数换算走 `POST /api/calc/splits`（归一化 + 最大余数法，合计恰为条数）；显示 = 预估 = 实际。

### 4.4 成本三档预估

- 接口：`POST /api/testset/estimate-cost`。
- 展示：`下限 / 预期 / 最坏` + 约 N 次 LLM 调用 + 入/出拆分 + 分项 breakdown + 分块统计 + 增强预估 + 多跳上下文放大倍数。
- 口径：下限 = 基础调用成本；预期 = 按标定系数放大；最坏 = 下限 × 条数 × 重试上限。
- 换 KG / 条数 / 比例 / 连线依据 / 人格数 / 重试上限后前端主动清空旧结果。

### 4.5 生成执行

- 不调 `TestsetGenerator.generate()`：人格 → 场景 Executor → 样本 Executor，`raise_exceptions=False`，每条样本外包记录器，完成一条记一条。
- 预检 `_precheck_kg`：摘要向量 / `entities` / 连线依据的边 / `entities_overlap` 边，缺就 **0 token** 报错。
- 终态：`stop_reason ∈ {completed, canceled, budget_exceeded, failed}` + `partial` + `failed_samples` + `error` + `undelivered`；**任何终态都先落盘再结束**。
  - 超预算有样本 → `succeeded` + `partial`；无样本或全部失败 → `failed`（错误里带已保存的测试集 id）。
- 用量解析用 `_token_usage_parser`，不依赖 ragas 的内置统计函数。

### 4.6 任务进度与取消

- 页面重载后可恢复跟踪（`useTrackedTask`）；`TaskProgress` 展示阶段、进度、日志、取消按钮。
- 取消 = `executor.cancel()` + `call_soon_threadsafe(main.cancel)`；取消后不再发新调用，**已花的在途费用不回收**。

### 4.7 样本审阅

- 题型徽章、维度/风格提示、`MOCK` 徽章、来源变化徽章、补生成徽章；`reference` 显示为「标准答案」（落盘与导出仍用 ragas 原键名）。
- 采纳切换：`PATCH /api/testset/samples/{index}`。

### 4.8 来源检查与局部补生成

- 打开详情后自动请求 `GET /api/testset/{id}/regenerate-plan` 与 `GET /api/testset/{id}/provenance`。
- `plan_regeneration` 对照数据源**当前投影**判定每条样本 `effective / changed / missing / unbound / unknown`，产出 `affected / index / blocked`，**0 token 预估**。
- 执行：`POST /api/testset/{id}/regenerate`，**另存为新测试集，旧测试集不改**。

### 4.9 质检报告

- `GET /api/testset/health/{kg_id}` 返回 `node_stats / edge_stats / prop_coverage / synthesizers / verdict / recommended`。
- 弹窗支持「按推荐重算」「当作本次连线依据」。
- 口径统一：`_kg_flags` 统计素材（文档级/分块级相似边分开计数、`entities_overlap` 分桶、`entities` 按非空判定），预检、能力判定、质检报告共用同一口径。
- **单跳具体题依赖 `entities` 而不是 `themes`**（ragas 的真实行为）。

### 4.10 导出

- 入口：`CSV · 全部` / `JSONL · 全部` / `JSONL · 仅采纳`。
- 接口：`POST /api/testset/export`，body `{format: csv|jsonl, scope: all|adopted|by_type, types}`。
- 实现是**手写 CSV / JSONL**（不是 ragas 原生序列化器），但字段层面与 ragas 兼容，可被 `Testset.from_list` 读回。
- 细节：CSV 公式注入防护；演示数据在文件名与内容上标注；空 `response` / `retrieved_contexts` 的处理。
- 已知坑：ragas 的 `from_jsonl` 不指定编码，Windows 读中文需 `PYTHONUTF8=1`。

### 4.11 测试集列表徽章

`MOCK` / 来源已变化 / 已停止跟踪 / 投影已删除 / 部分结果 / 失败已跳过。

### 4.12 外部导入（仅 API）

`POST /api/testset/generate-external`：`mode = kg_json | chunks | documents`。校验 `validate_external_kg()` 在任务创建前同步返回 400 `{error_code:"external_kg_invalid"}`（节点缺 `id` 或 `id` 重复等）。

---

## 5 质量评估页（`EvaluationPage.tsx`）

- 导入用例 JSON → 选 LLM / Embedding 预设 → 跑评测 → 展示五指标。
- 五指标（`ragas_adapter/evaluation.py`）：`faithfulness` / `answer_relevancy` / `context_precision` / `context_recall` / `answer_correctness`。
- 长任务与生成同套 POST → SSE → cancel 契约。
- 当前这一页的预设需要手填 ID（无下拉），与其它页「自动取默认预设」的体验不同。

---

## 6 横切能力

### 6.1 长任务契约

```
POST {资源}/generate   → { task_id }
GET  /api/tasks/{id}/stream   (SSE, 支持 Last-Event-ID 续传)
POST /api/tasks/{id}/cancel
GET  /api/tasks/{id}          (快照轮询兜底)
GET  /api/tasks               (列表)
```

- 兼容别名：`/api/kg/tasks/...`、`/api/testset/tasks/...`，与主路由同口径透传 `Last-Event-ID`。
- SSE 事件：`progress` / `done`，带事件序号；前端 `subscribeTask()` 在 SSE 断开时轮询兜底。
- 任务状态：`pending / running / cancel_requested / succeeded / failed / canceled / interrupted`。
- `stop_reason` 单独暴露（建图超预算时为 `budget_exceeded`，status 仍是 failed）。

### 6.2 任务租约（互斥）

- `manager.create(type, resources)` 按资源键互斥，冲突抛 `ResourceBusyError`。
- 建图租约键：`("ragas-generation", f"run:{run_id}")`；RAGFlow 同步与改绑共享 `ragflow_sync_resources(dataset_id, connection_id)`。

### 6.3 阶段化骨架（StagedJob）

- `backend/app/tasks/staged.py`：`Stage / JobContext / UsageLedger / JobExecutor / StagedJob`。
- 四终态 `completed / canceled / budget_exceeded / failed` **一律先落盘再结束**。
- **线程铁律**：工作线程只写日志缓冲，由主协程 `_drain_loop()` 消费（跨线程直接操作 asyncio 队列不安全）。
- 使用方：Step 1 建图、Step 2 生成、RAGFlow 同步、局部补生成。

### 6.4 断点与续跑

- 仅 Step 1 有断点；Step 2 无断点，靠「任何终态先落盘 + 局部补生成」补救。

### 6.5 演示模式与 mock 标记

- `RAGAS_DEMO=1` 或 `RAGAS_AVAILABLE=False` → 演示路径，0 token。
- 测试集演示样本打 `is_mock: true`；**判断真假看落盘 `is_mock`，不看 `mode`**。
- 健康检查 `mode` 字段回 `ragas | demo`。

### 6.6 错误体与错误码

- 统一错误体：`{ detail（中文）, error_code, extra? }`；上游错误另带 `upstream_status`。
- 通用错误码：`bad_request / unauthorized / forbidden / not_found / method_not_allowed / validation_error / rate_limited / internal_error / bad_gateway / service_unavailable`。
- 业务错误码示例：`write_token_required`、`write_token_invalid`、`external_kg_invalid`、`ragflow_connection_not_found`、`preset_unavailable`、`dataset_not_visible`、`ragflow_projection_conflict`、`ragflow_snapshot_unavailable`、`testset_not_found`。
- 前端 `ApiError` 携带 `message / detail / errorCode / upstreamStatus / endpointMissing`。
- 判断 RAGFlow 成功**不看 HTTP 200**：要 `application/json` + 可解析对象 + 业务 `code == 0`。

### 6.7 调试窗口

- `DebugDrawer.tsx` + `lib/errorLog.ts`：全局 `pushError(scope, message, detail)` / `pushInfo`，抽屉默认收起。
- 设计原则：**用户可见的失败不能只进抽屉**——关键表单错误就地渲染。

### 6.8 主题与语言

- 浅色为默认（背景 `#FAFAFA`、卡片 `#FFFFFF`、主色 `#5E6AD2`），深色由侧栏按钮切换（深色底 `#0A0A0B`、卡片 `#111113`），记在 `localStorage("ragas-theme")`，`index.html` 有首屏防闪脚本。
- 颜色全部使用 shadcn 语义名（`bg-background / bg-card / border-border / text-foreground / text-muted-foreground / bg-primary / text-primary-strong`）。
- 出题语言默认 `zh`，通过 `llm_context` **逐个合成器**注入（只在 `TestsetGenerator` 上设无效，因为本项目自建分布）。

### 6.9 单实例锁

- `data/.backend.lock` 写 PID；旧 PID 存活则拒绝启动，已死则接管（强杀残留锁会自动接管）。
- 原因：内存态文档索引是进程内 dict，多后端并发写 `index.json` 会互相覆盖。
- 生效时机：uvicorn 先跑 lifespan（建目录 → 锁 → `init_storage()`）再绑端口。

### 6.10 写令牌

- `run.py` 启动时生成写令牌，注入后端与前端环境变量；浏览器经 nginx / Vite 代理时自动带上，直连后端的脚本要自己带 `X-RAGAS-Write-Token`。
- 缺失/不匹配 → 403 `write_token_required` / `write_token_invalid`。
- 它只防「绕过代理直连后端」，**不是用户鉴权**；裸跑 uvicorn 时留空即不校验。

### 6.11 健康检查

`GET /api/health` 返回：

| 字段 | 含义 |
|---|---|
| `status` | `ok` |
| `mode` | `ragas` / `demo` |
| `app_id` / `workspace_id` / `instance_id` | 实例身份（启动器探活校验用） |
| `process_id` | 进程号 |
| `started_at` | 启动时间 |
| `commit` / `commit_note` | 代码版本提示 |
| `storage` | 存储健康摘要 |

### 6.12 CI 五项检查（`.github/workflows/ci.yml`）

| job | 内容 |
|---|---|
| `backend` | pytest（DATA_DIR 指临时目录） |
| `frontend` | ESLint + Vitest + `npm run build` |
| `e2e` | Playwright（演示模式，自起后端 + 前端，0 token） |
| `api-contract` | 后端 OpenAPI 与前端 `types.ts` 一致性 |
| `docker` | 后端与前端镜像构建 |

触发：`pull_request` + `push` 到主干；Python 3.13 / Node 22；**全部不需要密钥、不连外部服务**。

---

## 7 外部依赖与前置条件

### 7.1 LLM / Embedding

- 任何 OpenAI 兼容端点都可用；工作台不绑定供应商。
- 流式是可选能力，默认关闭（部分兼容网关只发布非流式接口，`stream=true` 会 400）；用 `RAGAS_STUDIO_LLM_STREAMING=1` 或预设 `extra.streaming` 开启。
- 请求体恒不带 `n` 字段（部分网关不接受，n>1 会拆成多次调用）。
- 个别兼容网关的 Embedding 一次只接受一条文本，遇该 400 会自动拆单条并发并逐条记账。
- DashScope 兼容层默认关「思考模式」；非 DashScope 端点一律不下发该字段。

### 7.2 RAGFlow

- 已在 `infiniflow/ragflow:v1.0.0` 上验证；本地默认 `127.0.0.1:9380`（以你的部署为准）。
- v1.0 文档级图谱必须用**带 Compiler 节点的摄取流水线**解析；仅置 `use_graphrag=true` 无效；库级图谱自动合并。
- 解析状态字段是 `ingestion_status`（完成值 `COMPLETED`）。
- 库不可见返回 HTTP 200 + 业务码（v1.0 为 `404/403/108`，v0.27.1 为 `102`）。
- `/test` 只执行前 4 段（connect/auth/datasets/documents），后三段恒 `skipped`；`probe` 七段只读抽样。

### 7.3 运行环境

- Python 3.13（`backend/.venv`）；Node ≥ 20.19。
- 首次安装需要 PyPI 与 npm registry；运行期只需要你配置的模型端点（演示模式连它都不需要）。
- Docker 可选，只用于 `docker compose up` 的体验路径。

---

## 8 已知边界

1. **取消不是瞬时计费停止**：Python 线程模型的硬限制——正在跑的 transform / 在途请求会跑完，结果丢弃，已花费用不回收。
2. **Step 2 无断点**：失败或取消后靠「已生成部分已落盘 + 局部补生成」补救。
3. **外部导入只有 API**：`kg_json / chunks / documents` 三种模式后端完整，前端没有入口。
4. **质量评估页预设要手填 ID**：与其它页的默认预设体验不一致。
5. **文档删除不可恢复**：物理删除优先，软删只是沙箱兼容兜底。
6. **单跳题永不跨文档**；边数只决定素材多少，不决定题目数。
7. **样本不记录来源文档 id**（`reference_contexts` 只有文本），跨文档题的事后精确验证依赖投影比对。
8. **出题风格不可配**：ragas 的四挡 `QueryStyle` 里三挡故意不通顺，属引擎行为，本项目未改。
9. **CSV / JSONL 导出是手写实现**：字段兼容 ragas，但不是 ragas 原生序列化器。
10. **单机单用户**：进程内索引 + 单实例锁，不支持多进程或多机并发写同一份 `data/`。
