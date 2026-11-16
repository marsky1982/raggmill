# raggmill

RAGgmill is a local-first RAG quality engineering platform that turns unstructured documents into quantifiable, traceable evaluation assets—grounded in knowledge graphs and measured by LLM-synthesized test sets. It lets teams systematically measure, diagnose, and improve their RAG systems without relying on real user-labeled data.

是一款本地优先（local-first）的 RAG 质量工程平台。它以知识图谱为骨架、以大模型合成测试集为标尺，将非结构化文档转化为可量化、可追溯、可回归的评测资产，帮助团队在没有真实用户标注数据的情况下，依然能够系统性地度量、诊断并持续优化检索增强生成（RAG）系统的实际效果。

在功能上，它是一条可视化流水线：

```
文档 → [Step 1] 知识图谱(KG) → [Step 2] 测试集 → 导出 CSV / JSONL
                                      ↘ [Step 3] 质量评估（可选）
```

分工上，**ragas 是引擎，raggmill 是驾驶舱**：本工具只做 [ragas](https://github.com/vibrantlabsai/ragas) SDK 的消费方，不修改 ragas 源码，也不重新实现出题算法。

- 无数据库：全部产物落在本地 `data/`（JSON + 上传副本），可直接用文件管理器查看、备份。
- 单机单用户：后端有单实例锁，避免多进程并发写同一份索引。
- 界面中文，浅色 Linear 风为默认，可切换深色。

---

## 能干什么

| 能力 | 说明 |
|---|---|
| 文档导入 | 上传 `.md / .html / .docx / .txt`（单文件、多文件、文件夹）；token 估算分桶提示 |
| 知识图谱 | Step 1 建图，力导向 SVG 预览（节点/关系分色、可缩放拖拽）；**每完成一个 transform 落盘断点**，中断后可续跑或放弃 |
| 测试集生成 | Step 2 按 单跳具体 / 多跳抽象 / 多跳具体 三类比例出题；生成前做能力预检与 token 三档预估；支持真取消、token 预算熔断、部分结果落盘 |
| 质检与补生成 | 质检报告给出素材覆盖与题型可用性、推荐多跳连线依据；内容变化后只补生成失效样本（另存为新测试集，旧的不改） |
| 导出 | CSV / JSONL（字段与 ragas 兼容，可被 `Testset.from_list` 读回）；CSV 带公式注入防护 |
| RAGFlow 对接 | 通过 HTTP 从 RAGFlow 知识库增量同步 chunk 与实体，投影成本地 KG 后直接出题（连接凭据加密存储，接口只回掩码） |
| 质量评估 | 可选第五步：导入用例 JSON，跑 Faithfulness / Answer Relevancy / Context Precision / Context Recall / Answer Correctness |
| 演示模式 | `RAGAS_DEMO=1` 用确定性假数据走完整两步流程，**0 token、0 外部服务**，用于体验与冒烟 |

功能细节（每个页面、每个接口、每个字段）见 [features/feature-map.md](features/feature-map.md)。

---

## 架构概览

```
浏览器（React 19 + TS + Tailwind v4 + shadcn/ui，Vite）
  │   REST(JSON) + SSE(进度/日志)      vite 代理 /api → 后端
  ▼
FastAPI 后端（Python 3.13）
  ├─ routers/           HTTP 入口，只做校验与任务编排
  ├─ tasks/             异步任务管理（真取消 + SSE + 任务租约）与统一阶段化骨架
  ├─ storage/           全部本地文件读写（预设/文档/KG/测试集/评测/RAGFlow）
  └─ ragas_adapter/     ragas SDK 封装（客户端、建图、出题、成本、导出、遥测）
        │ 进程内 import
        ▼
     ragas（引擎） → 外部 LLM / Embedding（OpenAI 兼容端点，用户自备）
```

三条硬规则：

1. **所有 ragas 调用只出现在 `backend/app/ragas_adapter/`**，路由层不直接 import ragas。
2. **没有数据库**，所有状态是可读的本地 JSON；`data/` 已在 `.gitignore` 里排除。
3. **长任务统一契约**：`POST` 起任务返回 `{task_id}` → `GET /api/tasks/{id}/stream`（SSE，支持 `Last-Event-ID` 续传）→ `POST /api/tasks/{id}/cancel`（真中断协程）。

更详细的实现说明见 [docs/design/architecture.md](docs/design/architecture.md)。

---

## 最快的上手路径

前置：**Python 3.13**、**Node ≥ 20.19**、npm。首次安装依赖需要访问 PyPI 与 npm registry。

### 方式 A：一键脚本

```powershell
# Windows
.\start.ps1                # 实模式（连你配置的 LLM/Embedding）
.\start.ps1 -Mode demo     # 演示模式（不连外部服务）
.\stop.ps1                 # 停止（按 PID + 进程身份校验）
```

```bash
# macOS / Linux
./start.sh        # demo 模式：RAGAS_DEMO=1 ./start.sh
./stop.sh
```

启动器会自动挑端口、把实际后端端口写进 `frontend/.env.local` 的 `VITE_API_TARGET`、把 PID 落盘，然后打开浏览器（默认前端 `http://127.0.0.1:5173`）。

### 方式 B：手动起前后端

```bash
# 后端（在 backend/ 下执行；默认 8100）
python -m venv .venv
.venv/Scripts/pip install -r requirements.lock     # Windows；Linux/macOS 用 .venv/bin/pip
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100

# 前端（另开一个终端）
cd frontend
npm ci
npm run dev
```

前端通过 Vite 代理 `/api` 到 `VITE_API_TARGET`（默认 `http://127.0.0.1:8100`，可写在 `frontend/.env.local`）。

### 方式 C：Docker

```bash
docker compose up -d --build      # 默认 http://127.0.0.1:8080（只绑本机）
docker compose logs -f backend
docker compose down               # data/ 保留
```

> 不要让 Docker 和原生方式同时指向同一份 `data/`：单实例锁按 PID 判断，容器与宿主机互相看不到对方 PID，会各自认为对方已退出并接管。

### 第一步做哪三件事

1. **模型配置**页：建 LLM 预设 + Embedding 预设，各设一个默认（见下一节）。
2. **文档**页：上传一两篇文档，看 token 估算。
3. **知识图谱**页「生成知识图谱」→ **测试集**页「生成测试集」→ 导出 CSV/JSONL。

健康检查：`GET /api/health` 返回 `{"status":"ok","mode":"ragas|demo", ...}`（另含实例身份、启动时间与存储健康摘要）。

---

## 预设怎么配

所有模型都走 **OpenAI 兼容协议**，在「模型配置」页建预设即可，不需要改代码。预设分三类：

| 类型（kind） | 用途 | 说明 |
|---|---|---|
| `llm` | Step 1 建图 | 摘要、主题、实体抽取等 transform |
| `llm_testset` | Step 2 出题 | 人格 / 场景 / 样本；未配置时回退到 `llm` 的默认预设 |
| `embedding` | 向量化 | 摘要向量与相似度边 |

每个预设要填：名称、`base_url`、`model`、`api_key`（Fernet 加密后存 `data/presets/`，接口只回掩码）。

DeepSeek 示例（默认推荐）：

```
kind       = llm / llm_testset
base_url   = https://api.deepseek.com/v1
model      = deepseek-chat
api_key    = <你的 key>
```

DeepSeek 目前不提供 embedding 接口，所以 `embedding` 预设需要另找 OpenAI 兼容的向量服务，例如：

```
base_url = https://<你的向量服务>/v1
model    = <embedding 模型名，如 bge-m3>
```

本地用 vLLM / Ollama 之类的 OpenAI 兼容服务起一个 embedding 模型同样可行，填对应 `base_url` 即可。

两个可选旋钮：

- **推理强度（reasoning_effort）**：留空 = 模型默认；可选 `none / minimal / low / medium / high`。带思考链的模型建议调到 `minimal` 或 `none`，能显著降低输出 token 与耗时。
- **思考模式（enable_thinking）**：**仅 DashScope 兼容端点可设**（默认关闭）。其它服务商收到该字段会返回 400，所以对非 DashScope 端点既不下发也不允许设置。

保存后点「连通自检」验证凭据；同 kind 的第一个预设自动成为默认。

---

## 演示模式

演示模式用确定性假数据走完「建图 → 出题 → 导出」，不连任何外部服务、不消耗 token，适合：

- 第一次打开先看界面长什么样；
- 改前端后做冒烟验证（`backend/tests` 之外的第二档验证）；
- 评审环境里没有可用 API key 时的兜底演示。

三种开启方式：`.\start.ps1 -Mode demo`、`RAGAS_DEMO=1 ./start.sh`、或 `RAGAS_DEMO=1` 后手动起后端。

演示数据的标记：图谱 `meta.mode = "demo"`，测试集样本 `is_mock: true`，导出文件名带「演示数据」后缀。**判断真假看 `is_mock`，不要看运行模式。**

---

## 验证怎么跑

三档，按环境能力选择：

### 1）必跑：离线自检（0 密钥、0 外部服务）

```powershell
.\verify.ps1            # Windows
```

```bash
./verify.sh             # macOS / Linux
```

它做四件事，全部使用临时数据目录，不碰你的 `data/`：

| 步骤 | 命令 | 基线 |
|---|---|---|
| 后端单测 | `pytest -q`（在 `backend/`，`DATA_DIR` 指向临时目录） | 579 passed |
| 前端 lint | `npm run lint` | 无 error |
| 前端单测 | `npm test`（Vitest） | 188 passed / 20 文件 |
| 前端构建 | `npm run build`（含 `tsc`） | 构建成功 |

也可以手工逐条执行：`cd backend && python -m pytest -q`；`cd frontend && npm run lint && npm test && npm run build`。

### 2）推荐：演示模式冒烟

`RAGAS_DEMO=1` 起后端（临时 `RAGAS_STUDIO_DATA_DIR`）→ 上传一篇文档 → 建图 → 出题 → 导出 CSV/JSONL，确认三个接口都返回成功、导出文件非空。判据：`/api/health` 的 `mode` 为 `demo`，任务终态为 `succeeded`，导出接口返回文件字节。

### 3）可选：端到端（Playwright）

```bash
cd frontend
npx playwright install --with-deps     # 首次需下载浏览器（约 150MB）
npm run e2e
```

e2e 会自起后端与前端（演示模式、0 token），数据目录用系统临时目录。**如果评审环境无法下载浏览器，跳过这一档即可**——前两档已经覆盖业务逻辑与界面构建。

---

## 目录结构

```
raggmill/
├── start.ps1 / stop.ps1 / start.sh / stop.sh   # 一键启停（run.py 是唯一实现）
├── run.py                     # 端口探测 + 前端代理 env + PID 落盘 + 单实例
├── docker-compose.yml         # 本地两容器起法（backend + nginx 前端）
├── verify.ps1 / verify.sh     # 一键自检（pytest / lint / vitest / build）
├── features/feature-map.md    # 功能地图：逐页能力、接口、落盘布局
├── docs/design/architecture.md# 技术架构（数据流、任务模型、断点、并发）
├── backend/
│   ├── app/
│   │   ├── main.py            # app 装配 + CORS + lifespan（建目录 → 单实例锁 → init_storage）
│   │   ├── config.py          # 路径常量、RAGAS_DEMO、ragas 可用性
│   │   ├── crypto.py          # api_key 的 Fernet 加密
│   │   ├── single_instance.py # data/.backend.lock（PID 探活/接管）
│   │   ├── routers/           # presets / documents / knowledge_graph / testset / tasks / calc / ragflow / evaluation
│   │   ├── storage/           # 全部落盘（base / presets / documents / kg / testsets / evaluations / ragflow）
│   │   ├── tasks/             # TaskManager（真取消 + SSE）与阶段化骨架 staged.py
│   │   └── ragas_adapter/     # ragas 封装：clients / kg / testset_* / telemetry / ragflow / entity_resolution
│   ├── tests/                 # pytest（临时 DATA_DIR，0 外部依赖）
│   └── requirements*.txt       # 依赖与锁定
├── frontend/
│   ├── src/pages/             # Config / Documents / Kg / Testset / Evaluation
│   ├── src/components/        # GraphView（自绘力导向 SVG）/ TaskProgress / DebugDrawer 等
│   ├── src/api.ts             # fetch + SSE 封装
│   └── e2e/                   # Playwright（演示模式）
└── data/                      # 运行时产物（gitignore）
    ├── presets/               # 预设（api_key 密文）
    ├── documents/             # 文档索引 + uploads/
    ├── kg/                    # KG JSON + checkpoints/（断点）
    ├── testset/               # 测试集 JSON（含 usage 账单）
    └── ragflow/               # 连接与快照
```

---

## 与 ragas 的关系

- 后端进程内 `import ragas`，直接使用 ragas 的 transforms、合成器与 `KnowledgeGraph`。

- 依赖钉在一个公开 fork：`GoldSharon/ragas@735726d5c`（Apache-2.0）。它相对上游补了出题相关的修复，**不是 PyPI 上的 0.2.x / 0.4.x**，升级前请先看 `backend/requirements.txt` 与 `requirements.lock`。

- Step 2 不调用 `TestsetGenerator.generate()` 这个黑盒，而是按相同顺序自行编排 人格 → 场景 → 样本，以获得取消、预算熔断、部分落盘与逐条记录能力——算法仍是 ragas 的。

- 不修改 ragas 源码；升级 ragas 只需要改 `ragas_adapter/` 这一层。

---

## 常见问题

**Q：启动后前端打不开 / 接口 404？**
先看后端是否活着：`Invoke-RestMethod http://127.0.0.1:8100/api/health`。前端读 `frontend/.env.local` 的 `VITE_API_TARGET`，端口不一致就改它。推荐直接用 `start.ps1` / `start.sh`，它会自动写对。

**Q：报「单实例锁被占用」？**
同机只能起一个后端（内存索引不可多进程共享）。正常停止用 `stop.ps1` / `stop.sh`；进程被强杀后残留锁会在下次启动时自动探活接管，不用手动删。

**Q：生成的题目有语病 / 中英夹杂？**
语病是 ragas 的 `QueryStyle` 设计（四挡里三挡故意不通顺），不是模型坏了；语言由「出题语言」下拉控制（默认中文），通过 `llm_context` 逐合成器注入。

**Q：token 花得比预估多很多？**
先看有没有重试（失败会把整份 prompt 重发），再看是否开着思考链——把「推理强度」调到 `minimal`/`none`。生成前的「预估 token 消耗」给的是下限/预期/最坏三档，最坏档按重试上限放大。

**Q：Windows 下用 ragas 读回导出的 JSONL 报编码错误？**
ragas 的 `from_jsonl` 不指定编码，读中文需要 `set PYTHONUTF8=1`。

**Q：模型自检很慢，正式出题却很快？**
自检走的是独立客户端，不一定套用「思考模式」等预设项；正式链路默认关闭 DashScope 的思考模式。

**Q：可以用别的模型吗？**
可以，任何 OpenAI 兼容端点都行——包括本地 vLLM / Ollama。这个工具不绑定任何供应商。

---

## License

[MIT](LICENSE)
