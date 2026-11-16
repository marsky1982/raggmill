import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearErrors, pushError, pushInfo, subscribeErrors } from "./errorLog";

let unsubscribe: (() => void) | null = null;

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  unsubscribe?.();
  unsubscribe = null;
  clearErrors();
  vi.restoreAllMocks();
});

describe("errorLog", () => {
  it("pushError 写入 error 级条目并通知订阅者", () => {
    const seen: string[] = [];
    unsubscribe = subscribeErrors((es) => seen.push(`${es[0]?.level}:${es[0]?.message}`));
    pushError("生成测试集", "接口 500", "traceback...");
    const latest = seen[seen.length - 1];
    expect(latest).toBe("error:接口 500");
  });

  it("pushInfo 写入 info 级条目，且不调用 console.error", () => {
    const seen: Array<{ level: string; detail?: string }> = [];
    unsubscribe = subscribeErrors((es) => seen.push({ level: es[0]?.level, detail: es[0]?.detail }));
    pushInfo("同步", "已同步 3 篇");
    expect(seen[seen.length - 1]).toEqual({ level: "info", detail: undefined });
    expect(console.error).not.toHaveBeenCalled();
  });

  it("新条目排在最前，且最多保留 200 条", () => {
    const lengths: number[] = [];
    unsubscribe = subscribeErrors((es) => lengths.push(es.length));
    for (let i = 0; i < 205; i++) pushInfo("压力", `第 ${i} 条`);
    expect(Math.max(...lengths)).toBe(200);
    let newestMessage = "";
    const probe = subscribeErrors((es) => { newestMessage = es[0].message; });
    probe();
    expect(newestMessage).toBe("第 204 条");
  });

  it("clearErrors 清空并通知", () => {
    const seen: number[] = [];
    unsubscribe = subscribeErrors((es) => seen.push(es.length));
    pushInfo("a", "1");
    clearErrors();
    expect(seen[seen.length - 1]).toBe(0);
  });

  it("取消订阅后不再收到通知", () => {
    let calls = 0;
    const off = subscribeErrors(() => { calls++; });
    off();
    pushInfo("a", "1");
    expect(calls).toBe(1); // 仅订阅时的首帧
  });
});
