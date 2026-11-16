import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const tracked = vi.hoisted(() => ({
  taskId: "task-1" as string | null,
  setTaskId: vi.fn(),
  apply: null as null | ((value: string | null) => void),
}));
// 有状态的 mock：setTaskId(null) 会真的清掉 taskId（与真实 useTrackedTask 行为一致），
// 否则进度卡片只是因为 mock 不清 taskId 才留在页面上，测不到「终态后收起」的真实表现。
vi.mock("../lib/useTrackedTask", async () => {
  const React = await import("react");
  return {
    useTrackedTask: () => {
      const [value, setValue] = React.useState(tracked.taskId);
      tracked.apply = setValue;
      return [value, tracked.setTaskId] as const;
    },
  };
});

type DoneEvent = { status: string; result?: Record<string, unknown>; error?: string; stop_reason?: string };
const sse = vi.hoisted(() => ({ handlers: null as null | { onDone: (d: DoneEvent) => void } }));
// 不 mock TaskProgress：三种终态由这里直接推 SSE 事件，覆盖「完成 / 失败 / 取消」的真实处理链路
vi.mock("../api", () => ({
  get: vi.fn(),
  post: vi.fn(),
  subscribeTask: vi.fn((_id: string, h: { onDone: (d: DoneEvent) => void }) => {
    sse.handlers = h;
    return () => {};
  }),
}));
vi.mock("../lib/errorLog", () => ({ pushError: vi.fn(), pushInfo: vi.fn() }));

import EvaluationPage from "./EvaluationPage";
import { get, subscribeTask } from "../api";

describe("EvaluationPage", () => {
  beforeEach(() => {
    tracked.taskId = "task-1";
    tracked.apply = null;
    tracked.setTaskId.mockReset();
    // 让清空 taskId 真的生效：页面据此收起进度卡片
    tracked.setTaskId.mockImplementation((next: string | null) => {
      tracked.taskId = next;
      tracked.apply?.(next);
    });
    sse.handlers = null;
    vi.mocked(subscribeTask).mockClear();
    vi.mocked(get).mockReset();
    vi.mocked(get).mockResolvedValue([] as never);
  });

  it("succeeded：出现 JSONL / CSV 导出链接，并清空任务 ID 收起进度", async () => {
    render(<EvaluationPage />);
    expect(screen.getByText("任务执行中")).toBeInTheDocument();

    act(() => { sse.handlers!.onDone({ status: "succeeded", result: { evaluation_id: "eval-1", is_mock: true } }); });

    expect(await screen.findByRole("link", { name: "JSONL" }))
      .toHaveAttribute("href", "/api/evaluations/eval-1/export?format=jsonl");
    expect(screen.getByRole("link", { name: "CSV" }))
      .toHaveAttribute("href", "/api/evaluations/eval-1/export?format=csv");
    expect(screen.getByText("评估 ID：eval-1")).toBeInTheDocument();
    // 演示模式的模拟分数必须在导出卡片上标出来
    expect(screen.getByTestId("mock-badge")).toHaveTextContent("MOCK 演示评分");
    expect(tracked.setTaskId).toHaveBeenCalledWith(null);
    // 轮询版本会在这里出现 GET /api/tasks/{id}（REVIEW：评估页曾自写 2.5s 轮询）
    expect(vi.mocked(get).mock.calls.filter(([url]) => String(url).startsWith("/api/tasks/"))).toEqual([]);
  });

  it("真实评分：导出卡片不带 MOCK 徽章", async () => {
    render(<EvaluationPage />);

    act(() => { sse.handlers!.onDone({ status: "succeeded", result: { evaluation_id: "eval-2" } }); });

    expect(await screen.findByRole("link", { name: "JSONL" })).toBeInTheDocument();
    expect(screen.queryByTestId("mock-badge")).not.toBeInTheDocument();
  });

  it("failed：错误条展示后端错误，且不出现导出链接", async () => {
    render(<EvaluationPage />);

    act(() => { sse.handlers!.onDone({ status: "failed", error: "模型额度不足" }); });

    expect(await screen.findByRole("alert")).toHaveTextContent("模型额度不足");
    expect(screen.queryByRole("link", { name: "JSONL" })).not.toBeInTheDocument();
    expect(tracked.setTaskId).toHaveBeenCalledWith(null);
  });

  it("canceled：进度卡片收起，不出导出链接，也不出错误条", async () => {
    render(<EvaluationPage />);
    expect(screen.getByTestId("task-id")).toHaveTextContent("task-1");

    act(() => { sse.handlers!.onDone({ status: "canceled" }); });

    // 真实页面上 onDone 会清 taskId，进度卡片随之收起，「已取消」不会停在页面上
    expect(tracked.setTaskId).toHaveBeenCalledWith(null);
    expect(screen.queryByTestId("task-id")).not.toBeInTheDocument();
    expect(screen.queryByText("已取消")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "JSONL" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("没有在跑的任务时不订阅 SSE", () => {
    tracked.taskId = null;
    render(<EvaluationPage />);
    expect(subscribeTask).not.toHaveBeenCalled();
  });
});
