import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { KgPreview } from "../types";
import GraphView from "./GraphView";

// 最小图谱：1 个文档 + 2 个分块，足以驱动布局与选中逻辑
const preview: KgPreview = {
  nodes: [
    {
      id: "doc-1", type: "document",
      properties: { page_content: "文档正文", document_metadata: { filename: "知识库说明.md" } },
    },
    { id: "chunk-1", type: "chunk", properties: { page_content: "分块一", themes: ["主题A"] } },
    { id: "chunk-2", type: "chunk", properties: { page_content: "分块二", themes: ["主题B"] } },
  ],
  relationships: [
    { id: "r1", type: "child", source: "doc-1", target: "chunk-1", bidirectional: false },
    { id: "r2", type: "child", source: "doc-1", target: "chunk-2", bidirectional: false },
    { id: "r3", type: "next", source: "chunk-1", target: "chunk-2", bidirectional: false },
  ],
  stats: {
    node_count: 3, relationship_count: 3,
    by_type: { document: 1, chunk: 2 }, total_nodes: 3, total_relationships: 3,
  },
};

beforeEach(() => {
  // jsdom 的 rAF 是 setTimeout 模拟：置为 no-op，避免仿真循环在测试期间异步改写 DOM。
  // 初始布局在 effect 里同步提交一次（setTick），不依赖 rAF，所以渲染与交互仍可测。
  vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 0);
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});
});

/** 节点可见圆（第一个 circle 是透明拾取区，第二个才是带填充/透明度的圆） */
function nodeCircle(container: HTMLElement, id: string): SVGElement {
  const g = container.querySelector(`g[data-node-id="${id}"]`);
  return g!.querySelectorAll("circle")[1] as unknown as SVGElement;
}

describe("GraphView", () => {
  it("渲染全部节点与图例", () => {
    const { container } = render(<GraphView data={preview} onSelect={vi.fn()} />);
    expect(container.querySelectorAll("g[data-node-id]").length).toBe(3);
    expect(screen.getByText("文档")).toBeInTheDocument();
    expect(screen.getByText("分块")).toBeInTheDocument();
    expect(screen.getByText("子级")).toBeInTheDocument();
    expect(screen.getByText("顺序")).toBeInTheDocument();
  });

  it("按下并抬起节点（无位移）→ 回调选中该节点", () => {
    const onSelect = vi.fn();
    const { container } = render(<GraphView data={preview} onSelect={onSelect} />);
    const root = container.firstElementChild as HTMLElement;
    const node = container.querySelector('g[data-node-id="chunk-1"]')!;
    fireEvent.pointerDown(node, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(root);
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect.mock.calls[0][0]).toMatchObject({ id: "chunk-1", type: "chunk" });
  });

  it("点画布空白（无位移）→ 回调清空选中", () => {
    const onSelect = vi.fn();
    const { container } = render(<GraphView data={preview} onSelect={onSelect} />);
    const root = container.firstElementChild as HTMLElement;
    fireEvent.pointerDown(root, { button: 0, clientX: 5, clientY: 5 });
    fireEvent.pointerUp(root);
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it("点图例区域不参与画布交互：不清空已选节点、不触发回调", () => {
    const onSelect = vi.fn();
    const { container } = render(<GraphView data={preview} onSelect={onSelect} />);
    const root = container.firstElementChild as HTMLElement;
    // 先选中 chunk-1：未选中的 chunk-2 圆会变暗（opacity 0.35）
    const selectedNode = container.querySelector('g[data-node-id="chunk-1"]')!;
    fireEvent.pointerDown(selectedNode, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(root);
    expect(nodeCircle(container, "chunk-2").getAttribute("opacity")).toBe("0.35");

    // 点图例空白（底板）：pointerdown 提前返回（不启动拖拽），pointerup 也不应改变选中
    const legendBase = container.querySelector("[data-legend] rect")!;
    fireEvent.pointerDown(legendBase, { button: 0 });
    fireEvent.pointerUp(root);
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(nodeCircle(container, "chunk-2").getAttribute("opacity")).toBe("0.35");
  });

  it("图例显示层不拦截指针：落在图例区的节点仍可点中（拾取底板在节点之下）", () => {
    const { container } = render(<GraphView data={preview} onSelect={vi.fn()} />);
    const svg = container.querySelector("svg")!;
    const firstNode = svg.querySelector("g[data-node-id]")!;
    // 拾取底板（data-legend）全部排在节点之前 → 绘制与命中都在节点之下
    for (const base of svg.querySelectorAll("[data-legend]")) {
      expect(base.compareDocumentPosition(firstNode) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    // 图例文字所在的显示层不接收指针，也不带 data-legend（否则会挡住其下的节点）
    for (const label of ["文档", "分块", "子级", "顺序"]) {
      const layer = screen.getByText(label).closest("g[pointer-events]");
      expect(layer?.getAttribute("pointer-events")).toBe("none");
      expect(screen.getByText(label).closest("[data-legend]")).toBeNull();
    }
  });

  it("按下后移动超过阈值（拖拽）→ 不触发选中", () => {
    const onSelect = vi.fn();
    const { container } = render(<GraphView data={preview} onSelect={onSelect} />);
    const root = container.firstElementChild as HTMLElement;
    const node = container.querySelector('g[data-node-id="chunk-2"]')!;
    fireEvent.pointerDown(node, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(root, { clientX: 30, clientY: 30 });
    fireEvent.pointerUp(root);
    expect(onSelect).not.toHaveBeenCalled();
  });
});
