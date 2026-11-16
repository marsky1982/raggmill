import { afterEach, describe, expect, it, vi } from "vitest";
import {
  del, dispositionFilename, fmtBytes, formatDetail, get, patch, post, postForBlob, put, subscribeTask, uploadFiles,
} from "./api";
import type { ApiError } from "./api";

/** 断言请求失败并取出 ApiError（catch 的 e 是 unknown，避免每处都写断言） */
async function expectApiError(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (e) {
    return e as ApiError;
  }
  throw new Error("期望请求失败，但成功返回了");
}

/** 构造一个只暴露 j() 用到字段的 Response 替身（避免依赖 jsdom 是否实现 Response） */
function jsonResponse(
  body: unknown,
  init: { status?: number; statusText?: string; jsonThrows?: boolean } = {},
): Response {
  const status = init.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: init.statusText ?? "",
    json: async () => {
      if (init.jsonThrows) throw new SyntaxError("Unexpected token <");
      return body;
    },
    headers: new Headers(),
    blob: async () => new Blob(["file"]),
  } as unknown as Response;
}

const stubFetch = (impl: (...args: Parameters<typeof fetch>) => Promise<Response>) => {
  const mock = vi.fn(impl);
  vi.stubGlobal("fetch", mock);
  return mock;
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("formatDetail", () => {
  it("空值返回空串", () => {
    expect(formatDetail(undefined)).toBe("");
    expect(formatDetail(null)).toBe("");
  });

  it("字符串原样返回", () => {
    expect(formatDetail("文档不存在")).toBe("文档不存在");
  });

  it("422 校验数组拼成「字段：原因」并去掉 body 前缀", () => {
    expect(formatDetail([
      { loc: ["body", "name"], msg: "Field required" },
      { loc: ["body", "kg_id"], msg: "不能为空" },
    ])).toBe("name：Field required；kg_id：不能为空");
  });

  it("无字段名的数组项只留原因", () => {
    expect(formatDetail([{ loc: ["body"], msg: "格式错误" }, "extra"])).toBe("格式错误；extra");
  });

  it("对象兜底为 JSON 字符串", () => {
    expect(formatDetail({ code: 403 })).toBe('{"code":403}');
  });
});

describe("请求错误封装", () => {
  it("成功时直接返回 JSON", async () => {
    stubFetch(async () => jsonResponse({ ok: true }));
    await expect(get<{ ok: boolean }>("/api/health")).resolves.toEqual({ ok: true });
  });

  it("业务 404 带 error_code 时不算 endpointMissing，并挂上 extra 里的上游状态", async () => {
    stubFetch(async () => jsonResponse(
      { detail: "测试集不存在", error_code: "testset_not_found", extra: { upstream_status: 502 } },
      { status: 404 },
    ));
    const err = await expectApiError(get("/api/testset/x"));
    expect(err.message).toBe("测试集不存在");
    expect(err.errorCode).toBe("testset_not_found");
    expect(err.upstreamStatus).toBe(502);
    expect(err.endpointMissing).toBe(false);
  });

  it("409 的 extra.sources 挂到 Error 上（KG 删除被数据源投影引用）", async () => {
    stubFetch(async () => jsonResponse(
      {
        detail: "知识图谱 kg-1 仍是数据源的当前投影",
        error_code: "kg_in_use",
        extra: { sources: [{ source_id: "conn__ds", dataset_id: "ds" }] },
      },
      { status: 409 },
    ));
    const err = await expectApiError(del("/api/kg/kg-1"));
    expect(err.errorCode).toBe("kg_in_use");
    expect(err.sources?.[0].source_id).toBe("conn__ds");
  });

  it("裸 404 Not Found 归为 endpointMissing（后端仍是旧进程）", async () => {
    stubFetch(async () => jsonResponse({ detail: "Not Found" }, { status: 404 }));
    const err = await expectApiError(get("/api/new-route"));
    expect(err.endpointMissing).toBe(true);
    expect(err.errorCode).toBeUndefined();
  });

  it("非 JSON 错误体退回 statusText", async () => {
    stubFetch(async () => jsonResponse(null, { status: 502, statusText: "Bad Gateway", jsonThrows: true }));
    const err = await expectApiError(get("/api/x"));
    expect(err.message).toBe("Bad Gateway");
    expect(err.endpointMissing).toBe(false);
  });

  it("有 error_code 但没有 detail 时保留完整错误体，便于排查", async () => {
    stubFetch(async () => jsonResponse({ error_code: "upstream_unreachable" }, { status: 502 }));
    const err = await expectApiError(get("/api/x"));
    expect(err.errorCode).toBe("upstream_unreachable");
    expect(err.message).toContain("upstream_unreachable");
  });
});

describe("写请求", () => {
  it("POST 序列化 JSON 并带 Content-Type", async () => {
    const fetchMock = stubFetch(async () => jsonResponse({ id: "t1" }));
    await post("/api/tasks", { name: "样例" });
    expect(fetchMock).toHaveBeenCalledWith("/api/tasks", expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({ "Content-Type": "application/json" }),
      body: JSON.stringify({ name: "样例" }),
    }));
  });

  it("body 省略时不写 body", async () => {
    const fetchMock = stubFetch(async () => jsonResponse({}));
    await post("/api/tasks/t1/cancel");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST", body: undefined });
  });

  it("PATCH / PUT / DELETE 用对应 method", async () => {
    const fetchMock = stubFetch(async () => jsonResponse({}));
    await patch("/api/p", { a: 1 });
    await put("/api/p", { a: 1 });
    await del("/api/p");
    expect(fetchMock.mock.calls.map((c) => (c[1] as RequestInit).method)).toEqual(["PATCH", "PUT", "DELETE"]);
  });

  it("设置 VITE_STUDIO_WRITE_TOKEN 时所有写请求带 X-RAGAS-Write-Token", async () => {
    vi.stubEnv("VITE_STUDIO_WRITE_TOKEN", "secret-token");
    vi.resetModules();
    const api = await import("./api");
    const fetchMock = stubFetch(async () => jsonResponse({}));
    await api.del("/api/p");
    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers["X-RAGAS-Write-Token"]).toBe("secret-token");
  });

  it("uploadFiles 用 FormData 并按相对路径命名（重名报错时能定位文件）", async () => {
    const fetchMock = stubFetch(async () => jsonResponse({ ok: true }));
    const files = [
      Object.assign(new File(["a"], "a.md"), { webkitRelativePath: "docs/a.md" }),
      new File(["b"], "b.md"),
    ] as File[];
    await uploadFiles(files);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const fd = init.body as FormData;
    expect(init.method).toBe("POST");
    expect(fd.getAll("files")).toHaveLength(2);
  });
});

describe("postForBlob", () => {
  it("成功时返回文件流与后端指定的文件名", async () => {
    const res = jsonResponse(null);
    (res as unknown as { headers: Headers }).headers = new Headers({
      "Content-Disposition": "attachment; filename*=UTF-8''%E6%B5%8B%E8%AF%95.csv",
    });
    stubFetch(async () => res);
    const { filename } = await postForBlob("/api/testset/export", { ids: ["1"] });
    expect(filename).toBe("测试.csv");
  });

  it("失败时走统一错误封装", async () => {
    stubFetch(async () => jsonResponse({ detail: "Not Found" }, { status: 404 }));
    const err = await expectApiError(postForBlob("/api/missing"));
    expect(err.endpointMissing).toBe(true);
  });
});

describe("dispositionFilename", () => {
  it("优先解析 RFC 5987 的中文文件名", () => {
    expect(dispositionFilename("attachment; filename=fallback.csv; filename*=UTF-8''%E7%A4%BA%E4%BE%8B.csv"))
      .toBe("示例.csv");
  });

  it("支持引号与不带引号的 ASCII 文件名", () => {
    expect(dispositionFilename('attachment; filename="ragas.csv"')).toBe("ragas.csv");
    expect(dispositionFilename("attachment; filename=plain.csv")).toBe("plain.csv");
  });

  it("无 header 返回 null", () => {
    expect(dispositionFilename(null)).toBeNull();
  });
});

describe("fmtBytes", () => {
  it("按 B / KB / MB 分档", () => {
    expect(fmtBytes(900)).toBe("900 B");
    expect(fmtBytes(1536)).toBe("1.5 KB");
    expect(fmtBytes(1048577)).toBe("1.0 MB");
  });

  it("边界值仍留在下一档（保持原行为）", () => {
    expect(fmtBytes(1024)).toBe("1024 B");
    expect(fmtBytes(1048576)).toBe("1024.0 KB");
  });
});

describe("subscribeTask", () => {
  class FakeEventSource {
    static instances: FakeEventSource[] = [];
    url: string;
    listeners = new Map<string, Array<(e: MessageEvent) => void>>();
    onerror: (() => void) | null = null;
    closed = false;
    constructor(url: string) {
      this.url = url;
      FakeEventSource.instances.push(this);
    }
    addEventListener(type: string, fn: (e: MessageEvent) => void) {
      const list = this.listeners.get(type) || [];
      list.push(fn);
      this.listeners.set(type, list);
    }
    close() {
      this.closed = true;
    }
    emit(type: string, payload: unknown) {
      for (const fn of this.listeners.get(type) || []) {
        fn({ data: JSON.stringify(payload) } as MessageEvent);
      }
    }
  }

  let close: (() => void) | null = null;

  const setup = (fetchImpl: () => Promise<Response>) => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const fetchMock = stubFetch(fetchImpl);
    const handlers = {
      onProgress: vi.fn(),
      onSnapshot: vi.fn(),
      onDone: vi.fn(),
    };
    close = subscribeTask("task-1", handlers);
    return { es: FakeEventSource.instances[FakeEventSource.instances.length - 1], fetchMock, ...handlers };
  };

  afterEach(() => {
    close?.();
    close = null;
    FakeEventSource.instances = [];
  });

  it("按 seq 去重，乱序到达不重复回调", () => {
    // 初始 poll 挂起，避免干扰事件断言
    const { es, onProgress } = setup(() => new Promise<Response>(() => {}));
    es.emit("progress", { status: "running", progress: 10, stage: "a", seq: 5 });
    es.emit("progress", { status: "running", progress: 20, stage: "a", seq: 7 });
    es.emit("progress", { status: "running", progress: 15, stage: "a", seq: 6 });
    expect(onProgress).toHaveBeenCalledTimes(2);
    expect(onProgress.mock.calls.map((c) => c[0].seq)).toEqual([5, 7]);
  });

  it("快照摊平 usage，且不推进去重序号", () => {
    const { es, onSnapshot, onProgress } = setup(() => new Promise<Response>(() => {}));
    es.emit("progress", { task_id: "task-1", status: "running", progress: 30, usage: { total_tokens: 120 }, seq: 99 });
    expect(onSnapshot).toHaveBeenCalledWith(expect.objectContaining({ total_tokens: 120, seq: 99 }));
    // 快照之后 seq=1 的实时事件仍应送达（断线重连回放不被当成重复）
    es.emit("progress", { status: "running", progress: 31, stage: "b", seq: 1 });
    expect(onProgress).toHaveBeenCalledTimes(1);
    expect(onProgress.mock.calls[0][0].seq).toBe(1);
  });

  it("done 事件触发 onDone 并关闭连接", () => {
    const { es, onDone } = setup(() => new Promise<Response>(() => {}));
    es.emit("done", { status: "succeeded", result: { count: 3 } });
    expect(onDone).toHaveBeenCalledWith({ status: "succeeded", result: { count: 3 } });
    expect(es.closed).toBe(true);
  });

  it("终态 progress 也会收尾（后端可能不额外发 done）", () => {
    const { es, onProgress, onDone } = setup(() => new Promise<Response>(() => {}));
    es.emit("progress", { status: "canceled", progress: 0, stage: "取消" });
    expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ status: "canceled" }));
    expect(onProgress).not.toHaveBeenCalled();
  });

  it("任务记录 404 时以 interrupted 收尾，提示核对已有结果", async () => {
    const { onDone } = setup(async () => jsonResponse({ detail: "Not Found" }, { status: 404 }));
    await vi.waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(onDone.mock.calls[0][0].status).toBe("interrupted");
    expect(onDone.mock.calls[0][0].error).toContain("服务可能已重启");
  });

  it("关闭函数之后不再回调", () => {
    const { es, onDone } = setup(() => new Promise<Response>(() => {}));
    close?.();
    close = null;
    es.emit("done", { status: "succeeded" });
    expect(onDone).not.toHaveBeenCalled();
  });
});
