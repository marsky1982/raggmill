import { describe, expect, it } from "vitest";
import { GRAPH_SOURCE_HINT, errorText, normBaseUrl, ragflowVersionText, verdictAdvice } from "./ragflowText";
import type { RagflowProbeResult } from "../types";

function errorWithCode(code?: string, upstreamStatus?: number | null): Error {
  return Object.assign(new Error("boom"), { errorCode: code, upstreamStatus });
}

function probe(overrides: Partial<RagflowProbeResult>): RagflowProbeResult {
  return {
    verdict: "ready",
    stages: [],
    counts: {},
    dataset_id: "ds-1",
    source_id: "src-1",
    request_count: 1,
    ...overrides,
  };
}

describe("normBaseUrl", () => {
  it("去掉首尾空白与末尾斜杠，保留路径", () => {
    expect(normBaseUrl("  http://127.0.0.1:9380//  ")).toBe("http://127.0.0.1:9380");
    expect(normBaseUrl("https://example.com/ragflow/")).toBe("https://example.com/ragflow");
  });
});

describe("ragflowVersionText", () => {
  it("原样显示接口返回的版本串（后端已剥掉 v 前缀，界面不补回、不截断）", () => {
    expect(ragflowVersionText("1.0.0")).toBe("RAGFlow 1.0.0");
    expect(ragflowVersionText("0.27.1")).toBe("RAGFlow 0.27.1");
    expect(ragflowVersionText(" v1.0.0-rc3 ")).toBe("RAGFlow v1.0.0-rc3");
  });

  it("拿不到版本时给明确文案，不留空白", () => {
    expect(ragflowVersionText(null)).toBe("版本未知");
    expect(ragflowVersionText(undefined)).toBe("版本未知");
    expect(ragflowVersionText("   ")).toBe("版本未知");
  });

  it("快照/投影的历史记录可换用未记录口径，与「探测不到」区分", () => {
    expect(ragflowVersionText(null, "快照未记录版本")).toBe("快照未记录版本");
    expect(ragflowVersionText("1.0.0", "快照未记录版本")).toBe("RAGFlow 1.0.0");
  });
});

describe("errorText", () => {
  it("四类 error_code 都给出可操作的中文提示", () => {
    expect(errorText(errorWithCode("upstream_unreachable"), "http://x")).toContain("连不上 RAGFlow：http://x");
    expect(errorText(errorWithCode("upstream_auth_failed"), "http://x")).toContain("API Key 无效");
    expect(errorText(errorWithCode("dataset_not_found"), "http://x")).toContain("重新选一个");
    expect(errorText(errorWithCode("upstream_not_json"), "http://x")).toContain("不是 RAGFlow 数据");
  });

  it("没有 error_code 时原样显示错误", () => {
    expect(errorText(new Error("超时"), "http://x")).toBe("Error: 超时");
  });

  it("upstream_error 带上游状态码时把状态码写进文案", () => {
    const text = errorText(errorWithCode("upstream_error", 404), "http://x");
    expect(text).toContain("RAGFlow 返回了错误响应（HTTP 404）");
    expect(text).toContain("路径前缀");
    // 与 not_json 区分：不是「返回的不是 RAGFlow 数据」
    expect(text).not.toContain("不是 RAGFlow 数据");
  });

  it("upstream_error 没有上游状态码时也能给出可操作提示", () => {
    const text = errorText(errorWithCode("upstream_error", null), "http://x");
    expect(text).toContain("RAGFlow 返回了错误响应：");
    expect(text).not.toContain("HTTP null");
    expect(text).toContain("后端日志");
  });
});

describe("verdictAdvice", () => {
  it("dataset_not_found 优先于 verdict 分支", () => {
    const text = verdictAdvice(probe({ verdict: "unreachable", error_code: "dataset_not_found" }), "http://x");
    expect(text).toContain("能列出知识库，但看不到该知识库");
  });

  it("unreachable / auth_failed / incompatible 各自指向具体排查动作", () => {
    expect(verdictAdvice(probe({ verdict: "unreachable" }), "http://10.0.0.1:9380")).toContain("http://10.0.0.1:9380");
    expect(verdictAdvice(probe({ verdict: "auth_failed" }), "http://x")).toContain("API Key 无效");
    expect(verdictAdvice(probe({ verdict: "incompatible" }), "http://x")).toContain("反向代理");
  });

  it("graph_empty 附带「只开 GraphRAG 开关不出图」的提示", () => {
    expect(verdictAdvice(probe({ verdict: "graph_empty" }), "http://x")).toContain(GRAPH_SOURCE_HINT);
  });

  it("partial 区分「提前停止 / 文档未解析完 / 文档无图谱」三种原因", () => {
    const stopped = verdictAdvice(probe({ verdict: "partial", stopped_reason: "max_requests" }), "http://x");
    expect(stopped).toContain("请求上限内提前停止");

    const unfinished = verdictAdvice(probe({ verdict: "partial", counts: { documents: 5, unfinished_documents: 2 } }), "http://x");
    expect(unfinished).toContain("有 2 篇文档还没解析完（该库共 5 篇）");

    const noGraph = verdictAdvice(probe({ verdict: "partial" }), "http://x");
    expect(noGraph).toContain("没有图谱");
  });

  it("ready 明确说明 7 项检查通过", () => {
    expect(verdictAdvice(probe({ verdict: "ready" }), "http://x")).toContain("7 项检查全部通过");
  });
});
