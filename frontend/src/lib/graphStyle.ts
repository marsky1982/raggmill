/** GraphView 的节点/边配色与中文名（纯映射，供图组件与测试共用）。 */

export const NODE_COLORS: Record<string, string> = { document: "#5E6AD2", chunk: "#3D9A6B" };

// 关系类型以真实 KG 为准（ ①：旧写法 ner_overlap 不存在，数量最多的实体重叠边全显示成灰色）
// 结构边改用主题的边框/次要文字色（浅色下自然变淡，深色下自动跟随，无新增色值）；
// 语义边保留主色与金色（视觉整改 R6 的边分层）
export const EDGE_COLORS: Record<string, string> = {
  child: "var(--border)", next: "var(--muted-foreground)", summary_similarity: "#5E6AD2", entities_overlap: "#B8863B",
};

export const SEMANTIC_EDGES = new Set(["summary_similarity", "entities_overlap"]);

// 中文类型/边名映射（用于图例与标签）
export const NODE_LABELS: Record<string, string> = { document: "文档", chunk: "分块" };
export const EDGE_LABELS: Record<string, string> = {
  child: "子级", next: "顺序", summary_similarity: "摘要相似", entities_overlap: "实体重叠",
};

// 旧版 demo 图谱写的是 ner_overlap：取色时按同义词处理，图例里不单列
const EDGE_ALIASES: Record<string, string> = { ner_overlap: "entities_overlap" };

/** 旧类型归一到当前类型（绘制分组与取色共用，避免图例出现别名项） */
export const normalizeEdgeType = (type: string) => EDGE_ALIASES[type] ?? type;

export const edgeColor = (type: string) => EDGE_COLORS[normalizeEdgeType(type)] || "var(--border)";
