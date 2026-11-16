import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DocItem, KgMeta, Preset } from "../types";

// 任务 id 由页面通过 useTrackedTask 管理：用可变状态 mock，便于构造「任务进行中」场景
const tracked = vi.hoisted(() => ({ taskId: null as string | null, setTaskId: vi.fn() }));
vi.mock("../lib/useTrackedTask", () => ({
  useTrackedTask: () => [tracked.taskId, tracked.setTaskId],
}));
// RAGFlow 面板是独立功能块，本文件只验证知识图谱页自身交互
vi.mock("../components/RagflowPanel", () => ({ default: () => null }));
vi.mock("../api", () => ({
  get: vi.fn(), post: vi.fn(), del: vi.fn(), subscribeTask: vi.fn(() => () => {}),
}));
vi.mock("../lib/errorLog", () => ({ pushError: vi.fn(), pushInfo: vi.fn() }));

import KgPage from "./KgPage";
import { del, get, post, subscribeTask } from "../api";

const api = {
  get: vi.mocked(get as unknown as (url: string) => Promise<unknown>),
  post: vi.mocked(post as unknown as (url: string, body?: unknown) => Promise<unknown>),
  del: vi.mocked(del as unknown as (url: string) => Promise<unknown>),
};
const subscribeMock = vi.mocked(subscribeTask as unknown as (id: string, h: unknown) => () => void);

const llmPreset: Preset = {
  id: "p-llm", kind: "llm", name: "默认 LLM", provider: "openai_compatible",
  base_url: "", model: "qwen-plus", extra: {}, created_at: "2026-11-01",
  revision: 1, is_default: true,
};
const embPreset: Preset = {
  id: "p-emb", kind: "embedding", name: "默认向量", provider: "openai_compatible",
  base_url: "", model: "text-embedding-v3", extra: {}, created_at: "2026-11-01",
  revision: 1, is_default: true,
};
const docs: DocItem[] = [
  {
    id: "doc-1", name: "说明.md", size: 2048, est_tokens: 617, chars: 1200,
    source: "upload", status: "ready",
  },
];

/** 按 URL 返回固定数据；presets/docs 由各用例按需覆盖 */
let presetsData: Preset[] = [llmPreset, embPreset];
let docsData: DocItem[] = docs;

/** RAGFlow 投影条目：只有 mode=ragflow-projection 的行才显示版本号 */
function projectionKg(version: string | null): KgMeta {
  return {
    id: "kg-rf-1", name: "RAGFlow 投影测试库", created_at: "2026-11-14 10:00",
    source_files: ["说明.md"], node_count: 3, relationship_count: 2,
    mode: "ragflow-projection", ragflow_version: version,
  };
}

/** 覆盖 /api/kg，用于验证列表行上的版本展示 */
function mockKgs(...kgs: KgMeta[]) {
  api.get.mockImplementation(async (url: string) => {
    if (url === "/api/documents") return docsData;
    if (url === "/api/presets") return presetsData;
    if (url === "/api/kg") return kgs;
    if (url === "/api/kg/checkpoints") return [];
    return [];
  });
}

function renderPage() {
  render(<KgPage onNext={vi.fn()} />);
}

/** 取 TaskProgress 注册的 SSE handlers，用于推进 run_id */
function lastHandlers() {
  const calls = subscribeMock.mock.calls;
  return calls[calls.length - 1]?.[1] as {
    onProgress?: (d: Record<string, unknown>) => void;
  };
}

describe("KgPage", () => {
  beforeEach(() => {
    tracked.taskId = null;
    presetsData = [llmPreset, embPreset];
    docsData = docs;
    api.get.mockImplementation(async (url: string) => {
      if (url === "/api/documents") return docsData;
      if (url === "/api/presets") return presetsData;
      if (url === "/api/kg") return [];
      if (url === "/api/kg/checkpoints") return [];
      return [];
    });
    api.post.mockResolvedValue({});
    api.del.mockResolvedValue({});
  });

  it("缺少默认 LLM/Embedding 时拦截生成", async () => {
    presetsData = [];
    renderPage();
    await screen.findByText("出题模型：未配置");
    fireEvent.click(screen.getByText("生成知识图谱（1 个文档）"));
    expect(screen.getByText("请先在「模型配置」创建并设好默认 LLM / Embedding 预设")).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("没有文档时拦截生成", async () => {
    docsData = [];
    renderPage();
    await screen.findByText("出题模型：默认 LLM（qwen-plus）");
    fireEvent.click(screen.getByText("生成知识图谱（0 个文档）"));
    expect(screen.getByText("请先在「文档输入」上传文档")).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("生成成功后提交参数并记录任务 id", async () => {
    api.post.mockResolvedValueOnce({ task_id: "task-9" });
    renderPage();
    await screen.findByText("出题模型：默认 LLM（qwen-plus）");
    fireEvent.click(screen.getByText("生成知识图谱（1 个文档）"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/api/kg/generate", expect.objectContaining({
      document_ids: ["doc-1"],
      llm_preset_id: "p-llm",
      embedding_preset_id: "p-emb",
      min_tokens: 500,
      max_tokens: 1000,
    }));
    expect(tracked.setTaskId).toHaveBeenCalledWith("task-9");
  });

  it("点取消先弹二次确认，选「保留断点」只取消任务", async () => {
    tracked.taskId = "task-1";
    renderPage();
    await screen.findByText("出题模型：默认 LLM（qwen-plus）");
    fireEvent.click(screen.getByText("取消"));
    expect(await screen.findByText("取消知识图谱生成？")).toBeInTheDocument();

    fireEvent.click(screen.getByText("取消任务（保留断点）"));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/tasks/task-1/cancel"));
    expect(api.del).not.toHaveBeenCalled();
  });

  it("选「取消并删除断点」时同时删除该次生成的断点", async () => {
    tracked.taskId = "task-1";
    renderPage();
    await screen.findByText("出题模型：默认 LLM（qwen-plus）");
    // 进度事件带回 run_id，删除断点的按钮才可用
    act(() => lastHandlers().onProgress?.({ status: "running", progress: 0.2, stage: "", run_id: "run-1" }));
    fireEvent.click(screen.getByText("取消"));
    fireEvent.click(await screen.findByText("取消并删除断点"));
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/api/kg/checkpoints/run-1"));
    expect(api.post).toHaveBeenCalledWith("/api/tasks/task-1/cancel");
  });

  it("投影 KG 行显示同步快照记录的 RAGFlow 版本", async () => {
    mockKgs(projectionKg("1.0.0"));
    renderPage();
    expect(await screen.findByTestId("kg-ragflow-version")).toHaveTextContent("RAGFlow 1.0.0");
  });

  it("存量投影没有版本时写「版本未记录」，不冒充真实版本", async () => {
    mockKgs(projectionKg(null));
    renderPage();
    expect(await screen.findByTestId("kg-ragflow-version")).toHaveTextContent("RAGFlow 版本未记录");
  });

  it("本地生成的图谱行不显示 RAGFlow 版本", async () => {
    mockKgs({ ...projectionKg("1.0.0"), mode: "local" });
    renderPage();
    await screen.findByText("RAGFlow 投影测试库");
    expect(screen.queryByTestId("kg-ragflow-version")).not.toBeInTheDocument();
  });
});
