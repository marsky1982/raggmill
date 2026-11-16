# 技术架构 — raggmill

> **文档状态**：2026-11，现行实现说明（v0.2 起按实际代码修订，已补 Step 2 分阶段编排后的结构）。
> 目标：单机本地部署、前后端分离、ragas 进程内调用；不追求多租户与分布式。

## 1 架构总览

单机本地部署，前后端分离。后端进程内直接 `import ragas`，不做远程调用——ragas 是引擎、后端负责编排、前端负责呈现。

```
┌─────────────────────────────────────────────────────────────┐
│                        浏览器（前端）                          │
│  React 19 + Vite + TS · 浅色 Linear 风格（.dark 分支可切深色）  │
│  ┌──────────┬───────────┬────────────┬────────────────────┐  │
│  │ 模型配置  │ 文档上传    │ 知识图谱预览 │ 测试集分类预览/导出  │  │
│  └──────────┴───────────┴────────────┴────────────────────┘  │
└───────────────┬───────────────────────────┬──────────────────┘
                │ REST (JSON)                │ SSE（流式进度/日志）
┌───────────────▼───────────────────────────▼──────────────────┐
│                     后端（FastAPI · Python）                    │
│  ┌────────────┐ ┌──────────────┐ ┌────────────────────────┐  │
│  │ 配置/预设   │ │ 文档加载      │ │ 任务调度器（异步/取消）  │  │
│  │ (密钥加密)  │ │ (langchain)   │ │ TaskManager + Executor │  │
│  └────────────┘ └──────────────┘ └───────────┬────────────┘  │
│  ┌───────────────────────────────────────────▼──────────────┐  │
│  │              ragas 适配层（ragas SDK 封装）                  │  │
│  │  build_llm() build_embeddings()  build_kg()  generate()   │  │
│  └───────────────────────────────────────────┬──────────────┘  │
└───────────────────────────────────────────────┼─────────────────┘
                                                │ 进程内调用
                                       ┌────────▼────────┐
                                       │  ragas (pip)    │
                                       │  KnowledgeGraph │
                                       └─────────────────┘
                                          外部 LLM/Embedding
                                       (用户配置的 endpoint)
```

### 设计要点

- **进程内调用 ragas**：无 RPC 序列化开销，直接持有 `KnowledgeGraph` 对象跨请求复用。
- **长任务异步**：生成是分钟级，走后台任务 + SSE 推进度，HTTP 请求立即返回 `task_id`。
- **密钥不出后端**：前端只存预设引用 id，密钥在后端加密存储/内存。
- **所有 ragas 调用只出现在 `ragas_adapter/`**：升级 ragas 只改这一层。

## 2 两步生成数据流

### Step 1：知识图谱

```
docs(List[Document])
  │
  ▼ KnowledgeGraph(nodes=[Node(DOCUMENT, page_content, metadata) for doc])
default_transforms(documents, llm, embedding_model)   ← 按 token 分箱选管线
  │
  ▼ apply_transforms(kg, transforms)   ← 每个 transform 一个 Stage，完成即落断点
  │   ├─ HeadlinesExtractor → HeadlineSplitter（>500 token 走此支）
  │   ├─ SummaryExtractor
  │   ├─ CustomNodeFilter（LLM 过滤低质 chunk）
  │   ├─ Parallel(EmbeddingExtractor, ThemesExtractor, NERExtractor)
  │   └─ Parallel(CosineSimilarityBuilder, OverlapScoreBuilder)
  ▼
kg  →  kg.save("kg.json")   ragas 原生 JSON 格式
```

- 多文件：`docs` 是列表，每文档一节点，ragas 内部统一处理，工具无需特殊处理。
- 断点续跑的基础就是「transform 粒度落盘」。

### Step 2：测试集

```
KnowledgeGraph.load("kg.json")        ← 复用，跳过 Step1
  │
  ▼ 自行分阶段编排（不调 TestsetGenerator.generate()）
  ├─ generate_personas_from_kg(...)                       ← 人格
  ├─ Executor: scenario.generate_scenarios(n=splits[i])    ← 场景（SSE 阶段）
  └─ Executor: synthesizer.generate_sample(scenario)       ← 样本（SSE 阶段）
  ▼
Testset(samples=[TestsetSample(eval_sample, synthesizer_name)])
  →  export_testset() → CSV / JSONL（手写实现，字段与 ragas 兼容）
```

- `calculate_split_values(probs, size)` 用归一化 + 最大余数法，合计恰为 `testset_size`；只提交 n>0 的合成器，场景截到 n。
- `raise_exceptions=False`，每条样本外包记录器，完成一条记一条；任何终态先落盘再结束。

## 3 长任务与流式进度

### 3.1 任务模型

后端 `TaskManager` 维护内存任务表（单机足够）：

| 字段 | 说明 |
|---|---|
| `task_id` | UUID |
| `type` | `build_kg` / `generate_testset` / `ragflow_sync` … |
| `status` | `pending/running/cancel_requested/succeeded/failed/canceled/interrupted` |
| `progress` | 0–1（粗粒度：阶段完成数/总阶段数） |
| `logs` | 有界队列（最近 N 条） |
| `result_ref` | 成功后指向产物（KG json 路径 / Testset id） |
| `stop_reason` | 终态原因（如建图超预算 `budget_exceeded`，status 仍是 failed） |
| `cancel_event` | asyncio.Event，取消置位 |

### 3.2 流式通道（SSE）

- `POST /api/kg/generate` → 立即返回 `{task_id}`。
- `GET /api/tasks/{task_id}/stream`（SSE）→ 推 `{status, progress, stage, log, run_id, node_count, ...}`；另有 `/api/kg/tasks/...`、`/api/testset/tasks/...` 兼容别名。
- 进度事件由 `ragas_adapter` 在阶段边界发布，SSE 支持 `Last-Event-ID` 续传，断开时前端退回轮询。
- 取消：`POST /api/tasks/{task_id}/cancel` → 置 `cancel_event` **并 `task._async_task.cancel()` 真正中断协程**。

### 3.3 取消的真实语义

- `TaskManager` 保存 `asyncio.create_task` 句柄（`task._async_task`）；`cancel()` 时：
  1. 置 `cancel_event`、任务状态置 `canceled`、立即向 SSE 推 `done`（前端秒级感知）；
  2. 调用 `_async_task.cancel()` 中断 `await`——包括 `asyncio.to_thread(...)` 这类阻塞调用（await 立即返回，底层线程跑完当前单元后结果被丢弃，不落盘、不阻塞新任务）。
- Step 2 取消 = `executor.cancel()` + `call_soon_threadsafe(main.cancel)`，取消后不再发起新调用；已完成样本与 token 账单照常落盘。
- Step 1 在 transform 之间检查 `cancel_event`，已完成的 transform 已落断点。
- 前端取消 KG 生成时二选一：仅取消（保留断点可续跑）/ 取消并删除断点。
- 已知限制：正在执行的 LLM 调用（transform 内部）会跑完，可能消耗少量 token；这是 Python 线程模型的硬限制，已花费用不回收。

### 3.4 断点续跑（Step 1）

- 每完成一个 transform，落盘一次：`data/kg/checkpoints/{run_id}.meta.json`（进度/文档/预设信息）+ `{run_id}.json`（KG 快照）。
- 失败时 meta 标 `status=failed` 并保留 `stage_index`；前端页面顶部出现「可恢复的生成断点」栏：
  - **从断点继续**：`POST /api/kg/generate {run_id, resume: true}`——以断点 meta 中的文档为准（而非前端当前列表），重建 transforms 后从 `stage_index` 续跑；
  - **放弃**：`DELETE /api/kg/checkpoints/{run_id}`。断点删除不联动删除文档（文档是全局资源，可能被历史 KG 引用）。
- resume 的两条拒绝路径：断点不存在/已放弃；断点关联文档已被删除。错误文案精确区分。
- 策略参数（token 预算 / 重试上限 / 超时 / 并发）排除在续跑签名之外：调整它们不会使断点失效。

### 3.5 Step 2 的进度与终态

Step 2 自 2026-10 起不再调 `TestsetGenerator.generate()` 这一锤子同步调用，而是由 `_PhasedGeneration` 在工作线程里分阶段驱动 人格 → 场景 → 样本，因此：

- 进度按「阶段 + 已完成样本数」上报，不再依赖捕获 ragas 内部 tqdm；
- 每个阶段由统一的 `StagedJob` 骨架驱动（`backend/app/tasks/staged.py`）：四终态 `completed / canceled / budget_exceeded / failed` **一律先落盘再结束**；
- **线程铁律**：工作线程只写日志缓冲，由主协程 `_drain_loop()` 消费；跨线程直接操作 asyncio 队列不安全；
- 终态字段：`stop_reason` / `partial` / `failed_samples` / `error` / `undelivered`；超预算但有样本 → `succeeded` + `partial`，无样本或全部失败 → `failed`（错误里带已保存的测试集 id）。

## 4 知识图谱预览方案

KG 落盘是纯 JSON（ragas `KnowledgeGraph.save`）。前端预览不依赖 ragas，只读 JSON：

```json
{
  "nodes": [
    {"id": "<uuid>", "type": "document|chunk", "properties": {"page_content": "...", "summary": "...", "themes": [...], "entities": [...], "summary_embedding": [...], ...}}
  ],
  "relationships": [
    {"id": "<uuid>", "type": "child|next|summary_similarity|entities_overlap", "source": "<node-uuid>", "target": "<node-uuid>", "bidirectional": false, "properties": {...}}
  ]
}
```

- **图渲染**：`GraphView` 自实现轻量力导向布局（斥力+引力+向心+阻尼，百级节点足够）的纯 SVG 渲染，未引入 reactflow。DOCUMENT/CHUNK 分色；边按 `type` 着色。支持缩放（+/−/⟲）与按住拖拽平移；拖拽超阈值自动屏蔽节点误点击。
- **节点详情**：右侧面板展示 `properties` 全部键；数组为数值时显示维度与样本前几维（embedding），否则以 `、` 连接。
- **统计**：节点数、关系数、按 type 分组计数。
- **关于图片**：本工具不考虑文档内图片。ragas KG 本就不含图片数据；用户先用 VLM 把图片处理成文本再喂入，预览纯文本+拓扑。

## 5 目录与文件布局（实际实现）

```
raggmill/
├── start.ps1 / stop.ps1 / start.sh / stop.sh   # 一键启停（run.py 是唯一实现）
├── run.py                     # 端口探测 + 前端代理 env + PID 落盘 + 单实例
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI app 装配 + CORS（localhost 任意端口）+ lifespan
│   │   ├── single_instance.py # 单实例锁（PID 文件 + 探活/接管）
│   │   ├── config.py          # 路径常量 + RAGAS_DEMO 模式
│   │   ├── crypto.py          # api_key Fernet 加密/解密
│   │   ├── errors.py          # 统一错误体与错误码
│   │   ├── routers/
│   │   │   ├── models.py      # /api/presets CRUD + set-default + test
│   │   │   ├── documents.py   # /api/documents upload/folder/estimate/delete
│   │   │   ├── knowledge_graph.py  # /api/kg generate/checkpoints/preview/delete
│   │   │   ├── testset.py     # /api/testset generate/samples/patch/export/health/regenerate
│   │   │   ├── tasks.py       # /api/tasks 状态/stream(SSE)/cancel
│   │   │   ├── calc.py        # /api/calc/splits 比例换算
│   │   │   ├── ragflow.py     # /api/ragflow 连接/数据集/probe/sync/快照
│   │   │   └── evaluation.py  # /api/evaluation 五指标评测
│   │   ├── storage/           # 全量本地存储（base / presets / documents / kg / testsets / evaluations / ragflow）
│   │   ├── tasks/
│   │   │   ├── manager.py     # TaskManager（create/run/cancel 真取消 + publish）
│   │   │   └── staged.py      # 统一阶段化任务骨架
│   │   └── ragas_adapter/
│   │       ├── clients.py     # build_llm / build_embeddings / 按事件循环建 HTTP 客户端
│   │       ├── kg.py          # Step 1 建图（StagedJob 阶段化 + 断点）
│   │       ├── testset_generation.py / testset_synth.py / testset_enhance.py
│   │       ├── testset_capabilities.py / testset_cost.py / testset_health.py
│   │       ├── testset_export.py / testset_external.py / testset_regenerate.py
│   │       ├── telemetry.py   # LLM 级 token/重试/失败采集
│   │       ├── ragflow.py     # RAGFlow 增量同步与投影
│   │       ├── entity_resolution.py  # 实体规范化与可选 LLM 消歧
│   │       └── doc_identity.py       # doc_id / source_ref 唯一构造处
│   └── requirements.txt / requirements.lock / requirements-dev.txt
├── frontend/
│   ├── src/
│   │   ├── pages/             # ConfigPage / DocumentsPage / KgPage / TestsetPage / EvaluationPage
│   │   ├── components/        # GraphView(力导向图) / TaskProgress(SSE) / DebugDrawer 等
│   │   ├── lib/               # errorLog / kgSelection 等
│   │   ├── api.ts             # fetch 封装 + SSE 客户端
│   │   └── types.ts
│   ├── .env.local             # VITE_API_TARGET（start.ps1 自动写入，vite 代理目标）
│   └── vite.config.ts         # /api -> VITE_API_TARGET 代理
└── data/                      # 运行时产物（gitignore）
    ├── presets/               # 预设 JSON（api_key 密文）
    ├── documents/             # index.json + uploads/（+ .trashed.json 墓碑名单）
    ├── kg/                    # KG JSON + checkpoints/{run_id}.meta.json|.json
    ├── testset/               # 测试集 JSON + 导出 csv/jsonl
    ├── ragflow/               # connections/ + sources/{source_id}/snapshots/
    └── .backend.lock          # 单实例锁（PID）
```

## 6 并发与资源

- **单实例锁（关键）**：`data/.backend.lock` 写 PID；重复启动时探活旧 PID，存活则拒绝启动（退出码 1），已死则接管。原因：内存态文档索引是进程内 dict，多实例并发写同一份 `index.json` 会互相覆盖。
- 单机单用户为主；多请求并发时 `TaskManager` 按资源键互斥（租约），避免同时跑两个重任务。
- 大文档：加载与切块在后台线程（`asyncio.to_thread`），避免阻塞事件循环。
- ragas Executor 自带并发（`max_workers`、`batch_size`），工作台把并发数收敛到策略对象统一管理。

### 6.1 存储删除语义

所有删除（文档/预设/KG/测试集/断点）遵循同一策略：

1. **优先物理删除**（`unlink`）——正常环境一步到位；
2. 被沙箱拦截（OSError）时**降级软删**：JSON 内容写 `_deleted: true` 标记，或（uploads）记入 `.trashed.json` 墓碑名单；读路径统一过滤 `_deleted`；
3. **启动时自动清扫**（`_purge_soft_deleted`）：物理删除所有历史软删文件与墓碑残留。

文档删除额外规则：同时删 uploads 源文件，防止重启后再次被扫描出来；重新上传同名文件自动清除墓碑。

## 7 错误与重试策略

| 场景 | 处理 |
|---|---|
| LLM/Embedding 连通失败 | 配置页自检即拦截，不给进 Step1 |
| 文档过短（<100 token） | 上传后预估 token，前端预拦 + 提示 |
| 单个 transform / sample 失败 | ragas `raise_exceptions=False` 容错收集；前端标红单条，可重试 |
| 任务超时 | 可配超时熔断，标 failed，保留已生成部分 |
| KG schema 漂移 | 钉死 ragas 版本；加载时校验 `nodes/relationships` 结构 |
| 重试放大成本 | 重试上限默认 3（ragas 原生默认 10），失败会把整份 prompt 重发 |

## 8 ragas 源码依据索引

| 结论 | 源码位置 |
|---|---|
| KG save/load 为 JSON | `testset/graph.py` `KnowledgeGraph.save/load` |
| 多文档→多 DOCUMENT 节点 | `generate.py` `generate_with_langchain_docs` |
| 默认 transforms 按 token 分箱 | `transforms/default.py` `default_transforms` |
| query_distribution 可自定义 | `generate.py` `generate(testset_size, query_distribution)` |
| 取消路径 | `generate(..., return_executor=True)` → `Executor.cancel()` |
| 导出方法集 | `dataset_schema.py` `RagasDataset` (`to_csv/to_jsonl/to_pandas/to_list`) |
| 分类依据 | `testset_schema.py` `TestsetSample.synthesizer_name` |
| KG 不含图片 | testset 模块 grep `image/base64/picture` 零命中 |
| 单跳具体题依赖 `entities` | `single_hop/specific.py` |
