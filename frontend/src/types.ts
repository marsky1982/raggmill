import type { components } from "./api-types";

/** 后端 OpenAPI 生成的响应模型（唯一来源，改后端模型后跑 `npm run gen:api`）。 */
type Schemas = components["schemas"];

export type Preset = Schemas["PresetOut"];

export type DocItem = Schemas["DocOut"];

export type KgMeta = Schemas["KgEntryOut"];

export type KgNode = Schemas["KgNodeOut"];

export type KgRel = Schemas["KgRelOut"];

export interface KgPreview {
  nodes: KgNode[];
  relationships: KgRel[];
  stats: {
    node_count: number;
    relationship_count: number;
    by_type: Record<string, number>;
    total_nodes: number;
    total_relationships: number;
  };
}

/** 单条样本：字段由 ragas Testset + 本项目后处理共同决定（模型开了 extra，新增字段不会被过滤） */
export type Sample = Schemas["SampleOut"];

export type TestsetDetail = Schemas["TestsetDetailOut"];

/** `GET /api/testset/{id}/regenerate-plan`：来源检查与局部补生成计划（只读、0 token） */
export interface RegeneratePlan {
  testset_id: string;
  /** 原测试集名称（补生成结果默认命名为「原名·局部补生成」） */
  testset_name?: string | null;
  judged_against_kg_id: string | null;
  /** current_projection = 数据源当前图谱；own_kg = 测试集自己的图（非 RAGFlow 或找不到当前图谱） */
  judged_against: "current_projection" | "own_kg" | "explicit";
  counts: { valid: number; stale: number; orphan: number; legacy: number; unknown: number };
  total: number;
  regenerate: number;
  regenerate_by_type: Record<string, number>;
  keep: number;
  keep_legacy: number;
  affected: { index: number; status: "stale" | "orphan"; type: string; reason?: string | null }[];
  blocked: string | null;
  estimate: { estimate_tokens?: { floor: number; expected: number; worst_case: number }; error?: string } | null;
  legacy_note: string | null;
}

/** 任务快照（`GET /api/tasks/{id}`）。result 的形状随任务类型变化 */
export type TaskView = Schemas["TaskOut"];

/** RAGFlow 连接的探测阶段（7 项固定顺序，后端不会省略，只会标 skipped） */
export type RagflowStage = Schemas["RagflowStage"];

/** 连接对象：只含 api_key_mask，永不含明文/密文 key（后端模型不开 extra，凭据进不来） */
export type RagflowConnection = Schemas["RagflowConnectionOut"];

export type RagflowDataset = Schemas["RagflowDatasetOut"];

export type RagflowDatasetList = Schemas["RagflowDatasetListOut"];

/** probe 任务结果：与连接自检共用 `RagflowCheckResult`（verdict 六枚举 + 恒 7 项 stages） */
export type RagflowProbeResult = Schemas["RagflowCheckResult"];

/** 数据源当前快照的摘要；`ragflow_version` 为 null 表示该快照没记录版本（历史快照），不是探测失败 */
export type RagflowSnapshot = Schemas["RagflowSnapshotOut"];

/** 同步任务结果里的同步报告；旧任务结果没有 report */
export interface RagflowSyncReport {
  read: { documents: number; chunks: number; unfinished_documents: number };
  source_quality: { chunks: number; chunks_with_entities: number };
  changes: {
    added_chunks: number; changed_chunks: number; deleted_chunks: number; deleted_documents: number;
    reused_projection: boolean; inherited_chunks: number; inherited_documents: number;
  };
  /** 实体口径统计；旧任务结果没有该字段 */
  entity_resolution?: RagflowEntityStats;
}

/** 实体规范化 / LLM 消歧开关的本次同步统计（关闭态只有 mode） */
export interface RagflowEntityStats {
  mode: string;
  /** 真实候选对数（全量 blocking） */
  pairs_observed?: number;
  /** 超出 max_pairs 被丢弃的对数 */
  pairs_truncated?: number;
  /** 实际送 LLM 的对数 */
  pairs_sent?: number;
  rule_hits?: number;
  llm_calls?: number;
  cache_hits?: number;
  unsure?: number;
  tokens?: { input: number; output: number };
}

/** `GET/PUT /api/ragflow/.../settings`：开关与生效状态 */
export type RagflowEntitySettings = Schemas["RagflowSettingsOut"];

/** `POST .../settings/estimate`：离线三档预估，0 请求 0 token */
export interface RagflowEntityEstimate {
  available: boolean;
  mode?: string;
  hint?: string;
  candidate_pairs?: number;
  rule_hits?: number;
  cache_hits?: number;
  /** 实际会送 LLM 的对数（已按 max_pairs 截断） */
  llm_pairs?: number;
  pairs_truncated?: number;
  estimated?: { low: number; expected: number; high: number };
  basis?: string;
}

export interface RagflowSyncResult {
  kg_id: string;
  reused?: boolean;
  stale_testset_ids?: string[];
  retained_kg_ids?: string[];
  removed_kg_ids?: string[];
  report?: RagflowSyncReport;
  /** 本次同步的实体口径统计（关闭态为 {mode:"disabled"}） */
  entity_resolution?: RagflowEntityStats;
}

/** `GET /api/testset/capabilities/{kg_id}` 里展示同步报告用到的字段 */
export interface KgCapabilities {
  synthesizers: Record<string, {
    label: string; available: boolean; effective_available: boolean; auto_enhanceable: boolean; reason: string;
    missing: string[];
  }>;
}

export type Checkpoint = Schemas["CheckpointOut"];
