import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { KgMeta, Preset } from "../types";

const tracked = vi.hoisted(() => ({ taskId: null as string | null, setTaskId: vi.fn() }));
vi.mock("../lib/useTrackedTask", () => ({
  useTrackedTask: () => [tracked.taskId, tracked.setTaskId],
}));
// 预览上一页选中的图谱（测试里从零开始）
vi.mock("../lib/kgSelection", () => ({ consumeSelectedKg: () => null }));
// 任务进度组件由独立用例覆盖；这里只验证页面交互
vi.mock("../components/TaskProgress", () => ({ default: () => null }));
vi.mock("../lib/errorLog", () => ({ pushError: vi.fn() }));
vi.mock("../api", () => ({
  get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn(), postForBlob: vi.fn(),
}));

import TestsetPage from "./TestsetPage";
import { del, get, patch, post, postForBlob } from "../api";

const api = {
  get: vi.mocked(get as unknown as (url: string) => Promise<unknown>),
  post: vi.mocked(post as unknown as (url: string, body?: unknown) => Promise<unknown>),
  patch: vi.mocked(patch as unknown as (url: string, body?: unknown) => Promise<unknown>),
  del: vi.mocked(del as unknown as (url: string) => Promise<unknown>),
  postForBlob: vi.mocked(postForBlob as unknown as (url: string, body?: unknown) => Promise<unknown>),
};

const kgs: KgMeta[] = [
  {
    id: "kg-1", name: "知识库", node_count: 33, relationship_count: 250,
    created_at: "2026-11-01", source_files: ["说明.md"], mode: "ragas",
  },
];
const presets: Preset[] = [
  {
    id: "p-llm", kind: "llm_testset", name: "出题模型", provider: "openai_compatible",
    base_url: "", model: "qwen-plus", extra: {}, created_at: "2026-11-01",
    revision: 1, is_default: true,
  },
  {
    id: "p-emb", kind: "embedding", name: "向量模型", provider: "openai_compatible",
    base_url: "", model: "text-embedding-v3", extra: {}, created_at: "2026-11-01",
    revision: 1, is_default: true,
  },
];
const tsList = [{ id: "ts-1", name: "旧测试集", count: 2, created_at: "2026-11-02" }];

const sampleA = {
  index: 0, synthesizer_name: "single_hop_specific", user_input: "问题一",
  reference: "答案一", reference_contexts: ["上下文一"], adopted: false,
};
const sampleB = {
  index: 1, synthesizer_name: "single_hop_specific", user_input: "问题二",
  reference: "答案二", reference_contexts: [], adopted: true,
};
const detail = {
  id: "ts-1", name: "旧测试集", total: 2,
  stats: { single_hop_specific: 2 },
  groups: { single_hop_specific: [sampleA, sampleB] },
  stop_reason: "completed",
};
const plan = {
  counts: { valid: 2, stale: 0, orphan: 0, legacy: 0 },
  regenerate: 0, keep: 2, regenerate_by_type: {},
  judged_against: "own_kg", testset_name: "旧测试集",
};
const health = {
  node_stats: {}, edge_stats: { by_type: {} },
  chunk_property_coverage: {}, synthesizer_availability: {},
};

let kgsData: KgMeta[] = kgs;

function renderPage() {
  render(<TestsetPage onBack={vi.fn()} />);
}

describe("TestsetPage", () => {
  beforeEach(() => {
    kgsData = kgs;
    api.get.mockImplementation(async (url: string) => {
      if (url === "/api/kg") return kgsData;
      if (url === "/api/presets") return presets;
      if (url === "/api/testset") return tsList;
      if (url === "/api/testset/languages") return [{ value: "zh", label: "中文" }];
      if (url.startsWith("/api/testset/capabilities/")) return {};
      if (url === "/api/testset/ts-1") return detail;
      if (url.includes("/regenerate-plan")) return plan;
      if (url.startsWith("/api/testset/health/")) return health;
      return {};
    });
    api.post.mockResolvedValue({ task_id: "task-1" });
    api.patch.mockResolvedValue({});
    api.del.mockResolvedValue({});
    api.postForBlob.mockResolvedValue({ blob: new Blob(["x"]), filename: "旧测试集-all.csv" });
    // jsdom 没有实现对象 URL 与下载链接行为：桩掉，只验证页面提示
    Object.defineProperty(URL, "createObjectURL", { value: vi.fn(() => "blob:x"), configurable: true });
    Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), configurable: true });
    // Radix 下拉/弹层依赖 ResizeObserver，jsdom 未实现
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
  });

  it("名称超过 80 字给即时错误并拦截生成", async () => {
    renderPage();
    await screen.findByText("已生成测试集（1）");
    const nameInput = screen.getByPlaceholderText("留空则按知识图谱名自动命名");
    // maxLength 只限制手动输入；直接赋值模拟粘贴超长名称
    fireEvent.change(nameInput, { target: { value: "名".repeat(81) } });
    expect(screen.getAllByText("测试集名称最多 80 个字符").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText("生成测试集"));
    expect(api.post).not.toHaveBeenCalled();
  });

  it("未选知识图谱时拦截生成", async () => {
    kgsData = [];
    renderPage();
    await screen.findByText("暂无。");
    fireEvent.click(screen.getByText("生成测试集"));
    expect(screen.getByText("请选择知识图谱（若无可回 Step 1 生成）")).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("生成提交参数，名称去除首尾空白并记录任务 id", async () => {
    renderPage();
    await screen.findByText("已生成测试集（1）");
    fireEvent.change(screen.getByPlaceholderText("留空则按知识图谱名自动命名"), {
      target: { value: "  我的测试集  " },
    });
    fireEvent.click(screen.getByText("生成测试集"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/api/testset/generate", expect.objectContaining({
      kg_id: "kg-1", llm_preset_id: "p-llm", embedding_preset_id: "p-emb",
      name: "我的测试集", language: "zh",
    }));
    expect(tracked.setTaskId).toHaveBeenCalledWith("task-1");
  });

  it("质检报告按所选图谱加载并弹出弹层，可关闭", async () => {
    renderPage();
    await screen.findByText("已生成测试集（1）");
    fireEvent.click(screen.getByText("质检报告"));
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/testset/health/kg-1"));
    // 弹层标题与按钮同名，这里按 dialog 容器断言，避免文案歧义
    await waitFor(() => expect(document.querySelector('[role="dialog"]')).not.toBeNull());
    fireEvent.click(screen.getByText("关闭"));
    await waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeNull());
  });

  it("删除测试集先确认：取消不调接口，确认后删除并刷新", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPage();
    await screen.findByText("旧测试集");
    fireEvent.click(screen.getByText("删除"));
    expect(api.del).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByText("删除"));
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/api/testset/ts-1"));
  });

  it("样本采纳按钮切换状态并调用 patch", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("旧测试集"));
    expect(await screen.findByText("问题一")).toBeInTheDocument();
    fireEvent.click(screen.getByText("丢弃"));
    await waitFor(() => expect(api.patch).toHaveBeenCalledWith(
      "/api/testset/ts-1/samples/0", { adopted: true },
    ));
    // 原始数据里第二条本就是已采纳，切换后两条都应显示「✓ 已采纳」
    expect(screen.getAllByText("✓ 已采纳")).toHaveLength(2);
  });

  it("关键词筛选只保留匹配样本", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("旧测试集"));
    await screen.findByText("问题一");
    fireEvent.change(screen.getByPlaceholderText("按关键词筛选 query…"), { target: { value: "问题二" } });
    expect(screen.queryByText("问题一")).toBeNull();
    expect(screen.getByText("问题二")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText("按关键词筛选 query…"), { target: { value: "不存在" } });
    expect(screen.getByText("无匹配样本。")).toBeInTheDocument();
  });

  it("仅看已采纳过滤未采纳样本", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("旧测试集"));
    await screen.findByText("问题二");
    fireEvent.click(screen.getByLabelText("仅看已采纳"));
    expect(screen.queryByText("问题一")).toBeNull();
    expect(screen.getByText("问题二")).toBeInTheDocument();
  });

  it("导出空内容时提示，不构造下载", async () => {
    api.postForBlob.mockResolvedValueOnce({ blob: new Blob([]), filename: "空.csv" });
    renderPage();
    fireEvent.click(await screen.findByText("旧测试集"));
    // Radix 下拉在 pointerdown 时展开（click 不够）
    fireEvent.pointerDown(await screen.findByText("导出"), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByText("CSV · 全部"));
    expect(await screen.findByText("导出内容为空，可能测试集无符合条件的数据")).toBeInTheDocument();
  });

  it("导出成功时用后端文件名提示下载", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("旧测试集"));
    fireEvent.pointerDown(await screen.findByText("导出"), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByText("CSV · 全部"));
    await waitFor(() => expect(api.postForBlob).toHaveBeenCalledWith(
      "/api/testset/ts-1/export", { format: "csv", scope: "all", types: undefined },
    ));
    expect(await screen.findByText(/已导出 旧测试集-all.csv（1 bytes）/)).toBeInTheDocument();
  });
});
