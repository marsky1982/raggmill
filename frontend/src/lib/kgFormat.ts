/** KG 详情面板的属性展示映射（纯逻辑，供页面与测试共用）。 */

/** 属性键名 → 中文显示名映射 */
export const PROP_LABELS: Record<string, string> = {
  page_content: "分块缩略内容",
  themes: "主题摘要",
  entities: "实体",
  keyphrases: "关键词",
  document_metadata: "文档元数据",
  filename: "文件名",
  embedding: "嵌入向量",
};

/** 格式化属性值为可读字符串 */
export function formatPropValue(v: unknown): string {
  if (Array.isArray(v)) {
    return v.length && typeof v[0] === "number"
      ? `向量[${v.length}] 示例: ${v.slice(0, 3).map((x) => Number(x).toFixed(3)).join(", ")}`
      : v.join("、");
  }
  if (typeof v === "object" && v !== null) return JSON.stringify(v);
  return String(v);
}
