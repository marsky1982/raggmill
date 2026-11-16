/** 实体规范化 / LLM 消歧开关的前端纯逻辑（可单测，不碰网络）。 */

/** 界面只暴露二态：关 = disabled，开 = hybrid（规则优先，规则判不了的才调 LLM）。 */
export const ENTITY_SWITCH_ON = "hybrid";
export const ENTITY_SWITCH_OFF = "disabled";

export const ENTITY_SWITCH_HINT_OFF = "关闭：仅做字符串规范化，不调用 LLM（默认）";
export const ENTITY_SWITCH_HINT_ON = "开启：规则优先，规则判断不了的再调用 LLM（消耗 token）";

export interface EntitySwitchContext {
  /** 当前选中的库（未选库时开关不可用） */
  hasDataset: boolean;
  /** 旧布局连接只读，不能改配置 */
  legacy: boolean;
  /** 该 source 正在同步或探测（避免边改边同步） */
  busy: boolean;
  /** 是否有可用的测试集生成用 LLM 预设 */
  hasPreset: boolean;
}

export interface EntitySwitchState {
  disabled: boolean;
  /** 禁用原因（中文）；可用时为 null */
  reason: string | null;
}

/** 开关禁用态：未选库 / legacy / 同步探测中 / 没有 llm_testset 预设。 */
export function entitySwitchState(ctx: EntitySwitchContext): EntitySwitchState {
  if (!ctx.hasDataset) return { disabled: true, reason: "先选一个知识库" };
  if (ctx.legacy) return { disabled: true, reason: "历史连接只读，不能修改配置" };
  if (ctx.busy) return { disabled: true, reason: "同步或检查进行中，结束后可修改" };
  if (!ctx.hasPreset) return { disabled: true, reason: "先到配置页添加测试集生成用 LLM 预设" };
  return { disabled: false, reason: null };
}

/** 生效状态文案：切换后不自动同步，只提示何时生效。 */
export function pendingText(pending: boolean, hasProjection = true): string | null {
  if (!pending) return null;
  return hasProjection ? "下次同步会重建投影" : "下次同步生效";
}

const MODE_LABELS: Record<string, string> = {
  disabled: "关闭",
  rules: "规则",
  llm: "仅 LLM",
  hybrid: "LLM 混合",
};

/** 实体口径标签：投影 / 测试集里的 mode 转中文，未知与缺失分开显示。 */
export function entityResolutionLabel(mode?: string | null): string {
  if (!mode) return "未记录";
  return MODE_LABELS[mode] || mode;
}

/** 从 settings 值推导开关是否打开（rules/llm 也视为打开，避免高级配置被界面误关）。 */
export function entitySwitchOn(mode?: string | null): boolean {
  return !!mode && mode !== ENTITY_SWITCH_OFF;
}

/** 本次同步的实体口径统计一行摘要；关闭态返回 null（界面不显示这一行）。 */
export function entityStatsText(stats?: {
  mode?: string; pairs_observed?: number; pairs_truncated?: number; pairs_sent?: number;
  rule_hits?: number; llm_calls?: number;
  cache_hits?: number; tokens?: { input: number; output: number };
} | null): string | null {
  if (!stats || !stats.mode || stats.mode === ENTITY_SWITCH_OFF) return null;
  const observed = stats.pairs_observed ?? 0;
  const truncated = stats.pairs_truncated ?? 0;
  const pairsText = truncated > 0
    ? `候选 ${observed} 对（截断 ${truncated}，送 LLM ${stats.pairs_sent ?? 0}）`
    : `候选 ${observed} 对`;
  const parts = [
    `口径：${entityResolutionLabel(stats.mode)}`,
    pairsText,
    `规则命中 ${stats.rule_hits ?? 0}`,
    `LLM ${stats.llm_calls ?? 0} 次`,
    `缓存命中 ${stats.cache_hits ?? 0}`,
  ];
  const tokens = (stats.tokens?.input ?? 0) + (stats.tokens?.output ?? 0);
  if (tokens > 0) parts.push(`约 ${tokens.toLocaleString("zh-CN")} token`);
  return parts.join("，");
}
