import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { SseHandlers } from "../api";

// SSE 订阅在组件里直接引用 subscribeTask：mock 掉它，由用例手动推进事件
vi.mock("../api", () => ({ subscribeTask: vi.fn(() => () => {}) }));
// 错误提示会写进全局调试抽屉，测试里不需要副作用
vi.mock("../lib/errorLog", () => ({ pushError: vi.fn(), pushInfo: vi.fn() }));

import TaskProgress from "./TaskProgress";
import { subscribeTask } from "../api";

/** 取最近一次订阅注册的 handlers（render 后 effect 已同步执行） */
function lastHandlers(): SseHandlers {
  const calls = vi.mocked(subscribeTask).mock.calls;
  return (calls[calls.length - 1]?.[1] ?? {}) as SseHandlers;
}

function renderTask(props: Partial<React.ComponentProps<typeof TaskProgress>> = {}) {
  const onDone = vi.fn();
  const onCancel = vi.fn();
  render(<TaskProgress taskId="task-1" onDone={onDone} onCancel={onCancel} {...props} />);
  return { onDone, onCancel, handlers: lastHandlers() };
}

describe("TaskProgress", () => {
  it("taskId 为 null 时不渲染、不订阅", () => {
    render(<TaskProgress taskId={null} onDone={vi.fn()} />);
    expect(screen.queryByRole("status")).toBeNull();
    expect(subscribeTask).not.toHaveBeenCalled();
  });

  it("SSE 进度事件渲染状态、阶段、步骤、节点统计与百分比", () => {
    const { handlers } = renderTask();
    act(() => handlers.onProgress?.({
      status: "running", progress: 0.42, stage: "分割文档",
      stage_index: 1, stage_total: 5, node_count: 12, relationship_count: 30,
    }));
    expect(screen.getByRole("status")).toHaveTextContent("任务执行中");
    expect(screen.getByText("阶段: 分割文档")).toBeInTheDocument();
    expect(screen.getByText("步骤 1/5")).toBeInTheDocument();
    expect(screen.getByText("12 节点 / 30 关系")).toBeInTheDocument();
    expect(screen.getByText("42%")).toBeInTheDocument();
    // 任务 ID 直接上卡片，排查时可直接抄走
    expect(screen.getByTestId("task-id")).toHaveTextContent("task-1");
  });

  it("英文阶段名按映射表显示中文，未知阶段原样显示", () => {
    const { handlers } = renderTask();
    act(() => handlers.onProgress?.({ status: "running", progress: 0.2, stage: "fetching" }));
    expect(screen.getByText("阶段: 拉取上游数据")).toBeInTheDocument();
    act(() => handlers.onProgress?.({ status: "running", progress: 0.3, stage: "brand_new_stage" }));
    expect(screen.getByText("阶段: brand_new_stage")).toBeInTheDocument();
  });

  it("心跳事件按普通日志行显示，阶段名走同一张中文映射表", () => {
    const { handlers } = renderTask();
    act(() => handlers.onProgress?.({
      status: "running", progress: 0.4, stage: "entity_resolution",
      heartbeat: true, stage_elapsed_s: 32, done: 2, total: 5,
    }));
    expect(screen.getByText("⏳ 仍在进行：实体规范化与消歧，已用时 32 秒，已完成 2/5")).toBeInTheDocument();
  });

  it("checkpoint 事件提示断点已保存", () => {
    const { handlers } = renderTask();
    act(() => handlers.onProgress?.({ status: "running", progress: 0.5, stage: "", checkpoint: true }));
    expect(screen.getByText("✓ 断点已保存")).toBeInTheDocument();
  });

  it("带遥测字段时渲染用量看板，超预算给出中止提示", () => {
    const { handlers } = renderTask();
    act(() => handlers.onProgress?.({
      status: "running", progress: 0.6, stage: "",
      llm_calls: 5, llm_calls_failed: 1, llm_retries: 2, llm_fix_calls: 1,
      input_tokens: 8000, output_tokens: 2000, total_tokens: 10000,
      elapsed_s: 125, budget_exceeded: true,
    }));
    expect(screen.getByText("累计 token")).toBeInTheDocument();
    expect(screen.getByText("10,000")).toBeInTheDocument();
    expect(screen.getByText("入 8,000 / 出 2,000")).toBeInTheDocument();
    expect(screen.getByText("已耗时 2m5s")).toBeInTheDocument();
    expect(screen.getByText("2 / 1")).toBeInTheDocument();
    expect(screen.getByText("⛔ 已超 token 预算，任务已自动中止")).toBeInTheDocument();
  });

  it("成功终态：显示任务完成并回传结果", () => {
    const { handlers, onDone } = renderTask();
    act(() => handlers.onDone?.({ status: "succeeded", result: { kg_id: "kg-1" } }));
    expect(screen.getByRole("status")).toHaveTextContent("任务完成");
    expect(screen.getByText("100%")).toBeInTheDocument();
    expect(onDone).toHaveBeenCalledWith({ kg_id: "kg-1" }, undefined, undefined);
  });

  it("失败终态：显示错误块并回传错误信息", () => {
    const { handlers, onDone } = renderTask();
    act(() => handlers.onDone?.({ status: "failed", error: "LLM 超时" }));
    expect(screen.getByRole("status")).toHaveTextContent("任务失败");
    expect(screen.getByText("任务执行失败")).toBeInTheDocument();
    expect(screen.getAllByText(/LLM 超时/).length).toBeGreaterThan(0);
    expect(onDone).toHaveBeenCalledWith(null, "LLM 超时", undefined);
  });

  it("超预算终态：stop_reason 透传给页面（status 仍是 failed）", () => {
    const { handlers, onDone } = renderTask();
    act(() => handlers.onDone?.({
      status: "failed", error: "已达到 token 预算，已保留断点，可从断点继续",
      stop_reason: "budget_exceeded",
    }));
    expect(onDone).toHaveBeenCalledWith(
      null, "已达到 token 预算，已保留断点，可从断点继续", "budget_exceeded",
    );
  });

  it("取消按钮只在运行中出现，并把进度事件里的 run_id 传给回调", () => {
    const { handlers, onCancel } = renderTask();
    act(() => handlers.onProgress?.({
      status: "running", progress: 0.1, stage: "", run_id: "run-9",
    }));
    fireEvent.click(screen.getByText("取消"));
    expect(onCancel).toHaveBeenCalledWith("run-9");
  });

  it("日志抽屉可收起/展开，日志内容跟随事件追加", () => {
    const { handlers } = renderTask();
    act(() => handlers.onProgress?.({ status: "running", progress: 0.2, stage: "", log: "▶ 步骤开始" }));
    expect(screen.getByText("▶ 步骤开始")).toBeInTheDocument();
    fireEvent.click(screen.getByText("收起日志"));
    expect(screen.queryByText("▶ 步骤开始")).toBeNull();
    fireEvent.click(screen.getByText("展开日志"));
    expect(screen.getByText("▶ 步骤开始")).toBeInTheDocument();
  });

  it("快照在日志为空时补回最近日志", () => {
    const { handlers } = renderTask();
    act(() => handlers.onSnapshot?.({
      task_id: "task-1", status: "running", progress: 0.3, stage: "", logs: ["■ 快照日志"],
    }));
    expect(screen.getByText("■ 快照日志")).toBeInTheDocument();
    expect(screen.getByText("30%")).toBeInTheDocument();
  });
});
