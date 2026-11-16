/**
 * 任务阶段名的唯一「英文 → 中文」映射表（PR9）。
 *
 * - 后端阶段名保持英文不变；前端只在这里翻译，查不到的阶段名原样显示，
 *   新增阶段不需要改组件、也不会报错。
 * - 建图的 transform 阶段由后端直接推中文标题（`kg.py` 的 `STAGE_DESC`），
 *   不要在这里再译一份，避免两份译名漂移。
 */
const STAGE_LABELS: Readonly<Record<string, string>> = Object.freeze({
  // RAGFlow 同步（ragas_adapter/ragflow.py 的五个 Stage）
  fetching: "拉取上游数据",
  entity_resolution: "实体规范化与消歧",
  projection: "生成图谱投影",
  inherit: "继承增强结果",
  commit: "提交同步结果",

  // 出题（testset_generation.py 的 Stage 与阶段内子阶段）
  enhance: "补齐图谱素材",
  enhancing: "补齐图谱素材",
  personas: "生成人格",
  scenarios: "生成场景",
  samples: "生成样本",
  kg_loaded: "知识图谱已加载",
  generating: "开始生成",
  accounting: "用量结算",
  plan: "生成补生成计划",

  // 外部导入（testset_external.py）
  kg_prepared: "外部图谱已准备",
  kg_building: "构建外部图谱",
  kg_built: "外部图谱构建完成",
  warn: "注意事项",

  // 演示模式（testset_demo.py / evaluation.py）
  warn_mock: "演示模式提示",
  warn_mock_done: "演示模式完成",
  demo_mock: "演示模式评分",

  // 通用生命周期（建图 / 出题 / 同步都会用到）
  init: "初始化",
  resume: "从断点续跑",
  done: "完成",
  completed: "完成",
  failed: "失败",
  canceled: "已取消",
  interrupted: "已中断",
  budget_exceeded: "超预算中止",
});

/** 阶段名转中文；未知阶段（含空值）按原样返回。 */
export function stageLabel(stage: string | null | undefined): string {
  if (!stage) return "";
  // 评估阶段是动态前缀 `metric:{key}`，按前缀统一译名（key 保持英文指标标识）
  if (stage.startsWith("metric:")) {
    const key = stage.slice("metric:".length).trim();
    return key ? `评估指标：${key}` : "评估指标";
  }
  const label = STAGE_LABELS[stage];
  return typeof label === "string" ? label : stage;
}

/** 心跳事件渲染成普通日志行：⏳ 仍在进行：{中文阶段}，已用时 X 秒，已完成 x/y */
export function formatHeartbeatLine(
  stage: string | null | undefined,
  elapsedS: number,
  done?: number,
  total?: number,
): string {
  const label = stageLabel(stage);
  const seconds = Math.max(0, Math.floor(elapsedS || 0));
  const head = label ? `⏳ 仍在进行：${label}` : "⏳ 仍在进行";
  const counts = total && total > 0 ? `，已完成 ${done ?? 0}/${total}` : "";
  return `${head}，已用时 ${seconds} 秒${counts}`;
}
