import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({
  get: vi.fn(), post: vi.fn(), put: vi.fn(), subscribeTask: vi.fn(() => () => {}),
}));
vi.mock("../lib/errorLog", () => ({ pushError: vi.fn(), pushInfo: vi.fn() }));
vi.mock("../lib/useTrackedTask", () => ({
  useTrackedTask: () => [null, vi.fn()],
}));

import RagflowPanel from "./RagflowPanel";
import { get } from "../api";

const api = {
  get: vi.mocked(get as unknown as (url: string) => Promise<unknown>),
};

const conn = { id: "c1", name: "本机 RAGFlow", base_url: "http://127.0.0.1:9380", api_key_mask: "rag***", legacy: false };
const datasetList = { items: [{ id: "kb1", name: "知识库A", doc_count: 2 }], total: 1 };

type MockOpts = {
  conn?: typeof conn;
  /** 快照接口返回；不传 = 404 snapshot_not_found（还没同步过） */
  snapshot?: unknown;
  /** 快照接口抛错时挂的错误码；不传 = snapshot_not_found */
  snapshotErrorCode?: string;
};

function mockApi(opts: MockOpts = {}) {
  api.get.mockImplementation(async (url: string) => {
    if (url === "/api/presets") return [];
    if (url === "/api/ragflow/connections") return [opts.conn ?? conn];
    if (url.startsWith("/api/ragflow/connections/c1/datasets?")) return datasetList;
    if (url.endsWith("/settings")) return { entity_resolution: "rules", llm_preset_id: null, pending_rebuild: null };
    if (url.endsWith("/snapshot")) {
      if (opts.snapshot) return opts.snapshot;
      const err = new Error("RAGFlow 源快照不存在") as Error & { errorCode?: string };
      err.errorCode = opts.snapshotErrorCode ?? "snapshot_not_found";
      throw err;
    }
    return [];
  });
}

function renderPanel() {
  render(<RagflowPanel busy={false} syncTaskId={null} setSyncTaskId={vi.fn()} onSyncDone={vi.fn()} />);
}

describe("RagflowPanel 已同步数据源的版本行", () => {
  beforeEach(() => { api.get.mockReset(); });

  it("显示上次同步快照记录的版本与时间", async () => {
    mockApi({ snapshot: { ragflow_version: "1.0.0", fetched_at: "2026-11-14 10:00" } });
    renderPanel();
    expect(await screen.findByTestId("ragflow-snapshot-version"))
      .toHaveTextContent("上次同步快照：RAGFlow 1.0.0 · 2026-11-14 10:00");
  });

  it("历史快照没有记录版本时不冒充真实版本", async () => {
    mockApi({ snapshot: { ragflow_version: null, fetched_at: "2026-11-01 09:00" } });
    renderPanel();
    expect(await screen.findByTestId("ragflow-snapshot-version")).toHaveTextContent("快照未记录版本");
  });

  it("快照不存在时明确写「还没同步过」，不留空白", async () => {
    mockApi();
    renderPanel();
    expect(await screen.findByTestId("ragflow-snapshot-version")).toHaveTextContent("该知识库还没有同步过快照");
  });

  it("读取报错（非 snapshot_not_found）时写读取失败，不冒充「还没同步」", async () => {
    mockApi({ snapshotErrorCode: "internal_error" });
    renderPanel();
    expect(await screen.findByTestId("ragflow-snapshot-version")).toHaveTextContent("快照版本读取失败");
  });

  it("legacy 连接直接读旧布局，不再打必然 404 的新布局接口", async () => {
    mockApi({
      conn: { ...conn, legacy: true },
      snapshot: { ragflow_version: "0.27.1", fetched_at: "2026-11-02 09:00" },
    });
    renderPanel();
    expect(await screen.findByTestId("ragflow-snapshot-version"))
      .toHaveTextContent("上次同步快照：RAGFlow 0.27.1");
    const urls = api.get.mock.calls.map((c) => String(c[0]));
    expect(urls).toContain("/api/ragflow/datasets/kb1/snapshot");
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(urls.some((u) => u.includes("/sources/"))).toBe(false);
  });
});
