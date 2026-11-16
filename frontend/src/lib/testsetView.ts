/** 测试集页的纯展示逻辑：题型/风格/终态的中文映射、条数分配与状态归类。
 *  单独放 lib 是为了能被 vitest 直接覆盖，也让页面组件只负责渲染。 */

export const DIST_KEYS = [
  { key: "single_hop_specific", label: "单跳·具体", desc: "SingleHopSpecificQuery —— 仅需一次实体检索即可回答的具体问题（如「产品 X 的价格是多少？」）", cls: "bg-primary" },
  { key: "multi_hop_abstract", label: "多跳·抽象", desc: "MultiHopAbstractQuery —— 需要跨多个实体推理、且答案为总结性/抽象性的问题（如「该产品的核心优势是什么？」）", cls: "bg-emerald-600" },
  { key: "multi_hop_specific", label: "多跳·具体", desc: "MultiHopSpecificQuery —— 需要跨多个实体检索、但答案为具体事实的问题（如「产品 X 和 Y 在参数 A 上的差异是多少？」）", cls: "bg-amber-600" },
] as const;

/** 测试集名称长度上限：与后端 backend/app/routers/testset.py 的 MAX_TESTSET_NAME_LEN 保持一致 */
export const MAX_TESTSET_NAME_LEN = 80;

/** 名称长度校验（纯逻辑，便于 vitest）。合法返回 null，超限返回中文提示。 */
export function testsetNameError(name: string): string | null {
  return name.trim().length > MAX_TESTSET_NAME_LEN
    ? `测试集名称最多 ${MAX_TESTSET_NAME_LEN} 个字符`
    : null;
}

/** ragas QueryStyle 的中文解释。bad=true 表示该风格「故意」生成不通顺的问题。
 *  枚举值见 ragas/testset/synthesizers/base.py 的 QueryStyle。 */
export const STYLE_HINT: Record<string, { label: string; tip: string; bad?: boolean }> = {
  PERFECT_GRAMMAR: { label: "通顺", tip: "Perfect grammar —— 唯一语法正常的风格" },
  "Perfect grammar": { label: "通顺", tip: "Perfect grammar —— 唯一语法正常的风格" },
  MISSPELLED: { label: "故意错别字", tip: "Misspelled queries —— ragas 故意写错字，模拟真实用户手误", bad: true },
  "Misspelled queries": { label: "故意错别字", tip: "Misspelled queries —— ragas 故意写错字，模拟真实用户手误", bad: true },
  POOR_GRAMMAR: { label: "故意病句", tip: "Poor grammar —— ragas 故意写语法错误的句子，模拟低水平提问", bad: true },
  "Poor grammar": { label: "故意病句", tip: "Poor grammar —— ragas 故意写语法错误的句子，模拟低水平提问", bad: true },
  WEB_SEARCH_LIKE: { label: "搜索关键词", tip: "Web search like queries —— 关键词堆叠，不成句", bad: true },
  "Web search like queries": { label: "搜索关键词", tip: "Web search like queries —— 关键词堆叠，不成句", bad: true },
};

/** 与后端 testset._STOP_LABELS 同义（终态 stop_reason 的中文） */
export const STOP_LABELS: Record<string, string> = {
  completed: "已完成", canceled: "已取消", budget_exceeded: "超预算中止",
  failed: "生成失败", undelivered: "条数不足",
};

/** 终态中文文案：缺失（历史数据）显示「未记录」，未知枚举显示「未知状态」——绝不把英文枚举直接显示出来 */
export const stopLabel = (reason?: string | null) => (reason ? STOP_LABELS[reason] || "未知状态" : "未记录");

export const TYPE_COLORS: Record<string, string> = {
  SingleHopSpecificQuery: "#5E6AD2",
  MultiHopAbstractQuery: "#3D9A6B",
  MultiHopSpecificQuery: "#B8863B",
};

/** 图表色只作图形填充（§3.1）；当字色时用同色相深色档。
 *  按 §3.1 的示例值在 10% 浅底上实测：单跳 5.00、抽象 4.69、具体 4.43（灰底卡片）；
 *  抽象与具体再压深一档后为 4.59 / 5.02，均 ≥ <对比度-正文> 4.5。
 *  深色主题下这三个深色档在 #111113 卡面上不足 4.5，改用同色相的浅色阶（不新增十六进制色值）。 */
export const TYPE_TEXT: Record<string, string> = {
  SingleHopSpecificQuery: "text-[#4A55C4] dark:text-indigo-300",
  MultiHopAbstractQuery: "text-[#2C754F] dark:text-emerald-300",
  MultiHopSpecificQuery: "text-[#805C1C] dark:text-amber-300",
};

export const REGEN_STATUS_LABEL: Record<string, string> = { stale: "内容已变", orphan: "来源已删除" };

export const REGEN_TYPE_LABEL: Record<string, string> = {
  single_hop_specific: "单跳具体", multi_hop_abstract: "多跳抽象", multi_hop_specific: "多跳具体",
};

export function regenStatusText(byStatus: Record<string, number>): string {
  return Object.entries(byStatus).map(([k, n]) => `${REGEN_STATUS_LABEL[k] || k} ${n}`).join("、") || "无";
}

export function matchType(synthName: string, distKey: string): boolean {
  const map: Record<string, string> = {
    single_hop_specific: "SingleHopSpecificQuery",
    multi_hop_abstract: "MultiHopAbstractQuery",
    multi_hop_specific: "MultiHopSpecificQuery",
  };
  return synthName === map[distKey];
}

/** 质检报告「可用 / 受限 / 其余」→ Badge 变体（后端 status 是中文枚举）。 */
export const statusVariant = (s: string) => (s === "可用" ? "success" : s === "受限" ? "warning" : "danger");

/** 比例归一化：和不为 1 时把每项按总和折算。
 *  取整口径与后端条数分配一致（归一化 + 最大余数法）：按 1/10000 为单位先取整，
 *  再把剩余的份额分给余数最大的项，保证回写后合计恰为 1（旧的 toFixed(4) 会得到 1.0001）。 */
export function normalizeDistribution(dist: Record<string, number>): Record<string, number> {
  const entries = Object.entries(dist);
  const sum = entries.reduce((a, [, v]) => a + v, 0);
  if (!(sum > 0)) return Object.fromEntries(entries.map(([k]) => [k, 0]));
  const UNITS = 10000;
  const parts = entries.map(([k, v]) => {
    const exact = (v / sum) * UNITS;
    const base = Math.floor(exact);
    return { k, units: base, rest: exact - base };
  });
  let remain = UNITS - parts.reduce((a, p) => a + p.units, 0);
  // 余数大的先补；相等时按 key 排序，保证结果稳定
  for (const p of [...parts].sort((a, b) => b.rest - a.rest || a.k.localeCompare(b.k))) {
    if (remain <= 0) break;
    p.units += 1;
    remain -= 1;
  }
  return Object.fromEntries(parts.map((p) => [p.k, p.units / UNITS]));
}
