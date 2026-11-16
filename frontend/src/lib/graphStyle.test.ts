import { describe, expect, it } from "vitest";
import { EDGE_LABELS, NODE_COLORS, NODE_LABELS, SEMANTIC_EDGES, edgeColor } from "./graphStyle";

describe("graphStyle", () => {
  it("语义边用主色/金色，结构边用主题变量", () => {
    expect(edgeColor("summary_similarity")).toBe("#5E6AD2");
    expect(edgeColor("entities_overlap")).toBe("#B8863B");
    expect(edgeColor("child")).toBe("var(--border)");
    expect(edgeColor("next")).toBe("var(--muted-foreground)");
  });

  it("旧 demo 图谱的 ner_overlap 走同义词映射", () => {
    expect(edgeColor("ner_overlap")).toBe(edgeColor("entities_overlap"));
  });

  it("未知类型退回边框色，不抛错", () => {
    expect(edgeColor("unknown")).toBe("var(--border)");
  });

  it("节点类型与边类型都有中文图例", () => {
    expect(NODE_COLORS.document).toBe("#5E6AD2");
    expect(NODE_LABELS.chunk).toBe("分块");
    for (const type of ["child", "next", "summary_similarity", "entities_overlap"]) {
      expect(EDGE_LABELS[type]).toBeTruthy();
    }
    expect(SEMANTIC_EDGES.has("summary_similarity")).toBe(true);
    expect(SEMANTIC_EDGES.has("child")).toBe(false);
  });
});
