"""API 契约类型：路径参数别名与 2xx 业务结果模型。

id 类路径参数的正则与 `storage.check_id` / `storage.validate_ragflow_name` 共用同一个常量
（名字只定义一处），非法值在请求解析阶段就被拦成 422 统一错误体，不用等业务代码抛 `UnsafePathError`。
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, ConfigDict

from . import storage
from .storage import ragflow as ragflow_store

# 落盘 id（preset / kg / run / testset / evaluation / doc / task）：单级安全名
SafeId = Annotated[str, Path(pattern=storage.SAFE_ID_PATTERN)]
# RAGFlow 的连接 id / 知识库 id：白名单多一个 `.`（见 storage.validate_ragflow_name）
RagflowId = Annotated[str, Path(pattern=ragflow_store.RAGFLOW_ID_PATTERN)]
# 数据源 id = connection_id + "__" + dataset_id，长度上限比单段大
RagflowSourceId = Annotated[str, Path(pattern=ragflow_store.RAGFLOW_SOURCE_ID_PATTERN)]

# 数据源解绑原因：connection_deleted（删除连接时写入）/ connection_missing（读取时推导）
SourceUnboundReason = Literal["connection_deleted", "connection_missing"]


class SelfCheckResult(BaseModel):
    """模型预设连通性自检（2xx 业务结果：连通失败不是请求错误）。"""

    ok: bool
    error_code: str | None = None
    detail: str | None = None
    latency_ms: int


class RagflowStage(BaseModel):
    """RAGFlow 探测的一个阶段（7 项固定顺序，后端不省略、只标 skipped）。"""

    key: str
    label: str
    status: Literal["ok", "fail", "skipped"]
    detail: str = ""
    upstream_status: int | None = None
    counts: dict[str, int] = {}
    checked_at: str | None = None


class RagflowCheckResult(BaseModel):
    """连接自检（test）与知识库探测（probe）的结果：请求成功，结论可能是「不可用」。"""

    verdict: Literal["ready", "partial", "graph_empty", "auth_failed", "incompatible", "unreachable"]
    stages: list[RagflowStage]
    request_count: int
    counts: dict[str, int] = {}
    dataset_id: str | None = None
    connection_id: str | None = None
    source_id: str | None = None
    stopped_reason: str | None = None
    detail: str | None = None
    error_code: str | None = None
    probed_version: str | None = None


# ---------- 稳定端点的 2xx 响应模型 ----------
# 约定：显式列出「当前真实返回」的每一个键，避免 response_model 静默过滤字段；
# 数据结构由外部来源决定（KG 条目、测试集条目、样本、任务快照）时开 extra="allow"，
# 保证新增字段不会因为没写进模型而消失。含凭据的模型（预设 / RAGFlow 连接）**绝不**开 extra：
# 宁可漏字段，也不允许 api_key / api_key_enc 透传。


class OkResult(BaseModel):
    """`{ok: true}` 类低信息量回执。"""

    ok: bool


class TaskRef(BaseModel):
    """起任务后的回执（长任务统一返回 task_id）。"""

    task_id: str


class LanguageOut(BaseModel):
    """出题语言下拉项。"""

    value: str
    label: str


class PatchSampleOut(BaseModel):
    """样本采纳标记的回执。"""

    ok: bool
    index: int
    adopted: bool


class RegenerateStartedOut(BaseModel):
    """局部补生成起任务后的回执：附带计划摘要，前端不用再查一次。"""

    task_id: str
    plan: dict[str, Any]


class PresetOut(BaseModel):
    """模型预设对外视图：只回掩码，永不含 api_key / api_key_enc。"""

    id: str
    # 与 storage.PRESET_KINDS 一致；写成字面量是为了让 OpenAPI 生成可收窄的联合类型
    kind: Literal["llm", "llm_testset", "embedding"]
    name: str
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    extra: dict[str, Any] = {}
    created_at: str = ""
    revision: int = 0
    is_default: bool = False
    api_key_mask: str | None = None


class PresetDefaultOut(BaseModel):
    ok: bool
    preset: PresetOut


class DocOut(BaseModel):
    id: str
    name: str
    size: int
    est_tokens: int
    chars: int
    source: str
    status: str


class DocTokenOut(BaseModel):
    name: str
    est_tokens: int


class DocEstimateOut(BaseModel):
    total_tokens: int
    avg_tokens: float
    bin: str
    warning: str | None = None
    per_file: list[DocTokenOut] = []


class KgEntryOut(BaseModel):
    """KG 索引条目。不同来源（本地 / RAGFlow 投影 / 外部导入）字段不完全一致，故允许额外字段。"""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str = ""
    created_at: str = ""
    llm_preset_id: str | None = None
    embedding_preset_id: str | None = None
    source_files: list[str] = []
    node_count: int = 0
    relationship_count: int = 0
    mode: str | None = None


class KgNodeOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    properties: dict[str, Any] = {}


class KgRelOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    source: str
    target: str
    bidirectional: bool | None = None
    properties: dict[str, Any] | None = None


class KgPreviewStats(BaseModel):
    node_count: int
    relationship_count: int
    by_type: dict[str, int] = {}
    total_nodes: int = 0
    total_relationships: int = 0


class KgPreviewOut(BaseModel):
    nodes: list[KgNodeOut]
    relationships: list[KgRelOut]
    stats: KgPreviewStats


class KgDetailOut(BaseModel):
    meta: KgEntryOut
    kg: dict[str, Any]


class KgDeletedOut(BaseModel):
    ok: bool
    detached_sources: list[dict[str, Any]] = []


class CheckpointOut(BaseModel):
    """Step 1 断点 meta（字段随断点格式演进，允许额外字段）。"""

    model_config = ConfigDict(extra="allow")

    run_id: str
    name: str = ""
    mode: str = ""
    status: str = ""
    stage_index: int = 0
    total_stages: int = 0
    doc_ids: list[str] = []
    doc_names: list[str] = []
    llm_preset_id: str | None = None
    embedding_preset_id: str | None = None
    node_count: int = 0
    relationship_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class CheckpointDeletedOut(BaseModel):
    ok: bool
    deferred: bool = False
    detail: str | None = None


class TaskOut(BaseModel):
    """任务快照（Task.view()）；result / usage / policy 形状随任务类型变化，这里按原样透传。"""

    model_config = ConfigDict(extra="allow")

    task_id: str
    type: str
    status: str
    progress: float = 0
    stage: str = ""
    logs: list[str] = []
    result: Any = None
    error: str | None = None
    stop_reason: str | None = None
    created_at: str = ""
    usage: dict[str, Any] = {}
    run_id: str | None = None
    resources: list[str] = []
    seq: int = 0
    workers_pending: int = 0
    policy: dict[str, Any] = {}


class SampleOut(BaseModel):
    """单条样本。字段集合由 ragas Testset + 本项目后处理共同决定，故允许额外字段。"""

    model_config = ConfigDict(extra="allow")

    index: int | None = None
    synthesizer_name: str = ""
    user_input: str = ""
    reference: str = ""
    reference_contexts: list[str] = []
    retrieved_contexts: list[str] | None = None
    response: str = ""
    adopted: bool = False
    is_mock: bool | None = None
    stale: bool | None = None
    source_chunk_ids: list[str] | None = None
    query_style: str | None = None
    query_length: str | None = None
    persona_name: str | None = None
    regenerated: bool | None = None
    legacy: bool | None = None


class TestsetEntryOut(BaseModel):
    """测试集索引条目（列表用）。存量条目字段随版本演进，允许额外字段。"""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str = ""
    created_at: str = ""
    kg_id: str = ""
    mode: str | None = None
    is_mock: bool | None = None
    stale: bool | None = None
    stale_sample_count: int | None = None
    source_dataset_id: str | None = None
    source_connection_id: str | None = None
    source_unbound: bool | None = None
    source_unbound_reason: SourceUnboundReason | None = None
    projection_missing: bool | None = None


class UndeliveredOut(BaseModel):
    """少出条数的明细拆分（存量测试集可能整块为 null）。"""

    model_config = ConfigDict(extra="allow")

    requested: int = 0
    planned: int | None = None
    delivered: int = 0
    not_planned: int | None = None
    not_delivered: int | None = None
    total_missing: int = 0


class UsageEnhancementOut(BaseModel):
    """出题前增强的用量分项。"""

    model_config = ConfigDict(extra="allow")

    total_tokens: int | None = None
    llm_tokens: int | None = None
    embedding_tokens: int | None = None
    llm_calls: int | None = None


class TestsetUsageOut(BaseModel):
    """token 账单；分项随口径演进，允许额外字段。"""

    model_config = ConfigDict(extra="allow")

    total_tokens: int | None = None
    llm_calls: int | None = None
    enhancement: UsageEnhancementOut | None = None


class RegenerationOut(BaseModel):
    """局部补生成的来源与替换范围（普通测试集为 null）。"""

    model_config = ConfigDict(extra="allow")

    from_testset_id: str
    from_testset_name: str | None = None
    replaced_indices: list[int] = []
    replaced_by_status: dict[str, int] = {}
    kept: int = 0
    requested: int = 0
    regenerated: int = 0


class SourceProjectionConfigOut(BaseModel):
    """生成时数据源投影的配置快照（含实体口径）；其余键随口径演进，允许额外字段。"""

    model_config = ConfigDict(extra="allow")

    entity_resolution: str | None = None


class TestsetDetailOut(BaseModel):
    """测试集详情：键集合与路由里的构造逐项对齐（含账单 / 少出条数 / 补生成 / 数据源跟踪）。"""

    id: str
    name: str
    created_at: str
    kg_id: str
    total: int
    groups: dict[str, list[SampleOut]]
    stats: dict[str, int]
    mode: str | None = None
    is_mock: bool = False
    stale: bool = False
    stale_sample_count: int = 0
    llm_preset_id: str | None = None
    llm_model: str | None = None
    embedding_preset_id: str | None = None
    embedding_model: str | None = None
    relation_property: str | None = None
    testset_size: int | None = None
    stop_reason: str | None = None
    partial: bool = False
    failed_samples: int = 0
    undelivered: UndeliveredOut | None = None
    error: str | None = None
    usage: TestsetUsageOut = TestsetUsageOut()
    regeneration: RegenerationOut | None = None
    source_dataset_id: str | None = None
    source_connection_id: str | None = None
    source_unbound: bool = False
    source_unbound_reason: SourceUnboundReason | None = None
    projection_missing: bool = False
    source_projection_version: str | None = None
    source_projection_config: SourceProjectionConfigOut | None = None


class RagflowLastTest(BaseModel):
    at: str
    verdict: str
    stages: list[RagflowStage] = []


class RagflowConnectionOut(BaseModel):
    """连接对外视图：只回掩码，永不含明文/密文 key；不开 extra，防止密文透传。"""

    id: str
    name: str = ""
    base_url: str = ""
    api_key_mask: str = ""
    declared_version: str | None = None
    probed_version: str | None = None
    last_test: RagflowLastTest | None = None
    legacy: bool = False
    dataset_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class RagflowDatasetOut(BaseModel):
    id: str
    name: str = ""
    doc_count: int | None = None
    chunk_count: int | None = None
    # 上游 status 老版本给字符串（"1"）、新版本给整数，故两者都接受；缺失时为 null
    status: str | int | None = None


class RagflowDatasetListOut(BaseModel):
    items: list[RagflowDatasetOut]
    total: int
    page: int
    page_size: int
    has_more: bool


class RagflowConnectionDeletedOut(BaseModel):
    deleted: bool
    retained: bool
    unbound_testset_ids: list[str] = []


class RagflowSyncStartedOut(BaseModel):
    task_id: str
    source_id: str


class RagflowManualSyncStartedOut(BaseModel):
    task_id: str
    mode: str


class RagflowSettingsOut(BaseModel):
    entity_resolution: str
    llm_preset_id: str | None = None
    config_fingerprint: str = ""
    applied_fingerprint: str | None = None
    pending_rebuild: bool = False


class RagflowEstimateOut(BaseModel):
    """离线预估：available=false 时只有 mode/hint，字段时间戳随估算口径演进故允许额外字段。"""

    model_config = ConfigDict(extra="allow")

    available: bool
    mode: str | None = None
    hint: str | None = None
    candidate_pairs: int | None = None
    rule_hits: int | None = None
    cache_hits: int | None = None
    llm_pairs: int | None = None
    pairs_truncated: int | None = None
    estimated: dict[str, Any] | None = None
    basis: str | None = None


class RagflowSnapshotOut(BaseModel):
    """源快照摘要（连接模式带 source_id/connection_id，legacy 端点没有这两项）。"""

    source_id: str | None = None
    dataset_id: str | None = None
    connection_id: str | None = None
    snapshot_id: str | None = None
    projection_kg_id: str | None = None
    projection_version: str | None = None
    fetched_at: str | None = None
    source_hash: str | None = None
    document_count: int = 0
    chunk_count: int = 0
    # 该快照记录的 RAGFlow 版本；历史快照可能没有该字段（界面按「未记录」展示，不冒充真实版本）
    ragflow_version: str | None = None
