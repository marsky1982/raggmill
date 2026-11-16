import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Preset, RagflowConnection } from "../types";

// 页面直接用 api 层发请求：整体 mock，按 URL 回固定数据
vi.mock("../api", () => ({
  get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn(),
}));
// 校验失败只进全局调试抽屉，这里断言调用参数即可（不渲染抽屉）
vi.mock("../lib/errorLog", () => ({ pushError: vi.fn(), pushInfo: vi.fn() }));

import ConfigPage from "./ConfigPage";
import { del, get, patch, post, put } from "../api";
import { pushError } from "../lib/errorLog";

// vitest 的 mocked 对泛型函数不友好，这里按「URL → 未知返回值」的宽松签名取 mock
const api = {
  get: vi.mocked(get as unknown as (url: string) => Promise<unknown>),
  post: vi.mocked(post as unknown as (url: string, body?: unknown) => Promise<unknown>),
  put: vi.mocked(put as unknown as (url: string, body?: unknown) => Promise<unknown>),
  patch: vi.mocked(patch as unknown as (url: string, body?: unknown) => Promise<unknown>),
  del: vi.mocked(del as unknown as (url: string) => Promise<unknown>),
};
const pushErrorMock = vi.mocked(pushError);

const presets: Preset[] = [
  {
    id: "p1", kind: "llm", name: "默认模型", provider: "openai_compatible",
    base_url: "https://api.example.com/v1", model: "qwen-plus", api_key_mask: "sk-***",
    extra: { temperature: 0.2 }, created_at: "2026-11-01", revision: 1, is_default: true,
  },
  {
    id: "p2", kind: "llm", name: "备用模型", provider: "openai_compatible",
    base_url: "", model: "qwen-max", extra: {}, created_at: "2026-11-02",
    revision: 1, is_default: false,
  },
];

const conns: RagflowConnection[] = [
  {
    id: "c1", name: "本机 RAGFlow", base_url: "http://127.0.0.1:9380",
    api_key_mask: "rag***", legacy: false,
  },
];

/** 预设表单的文本框顺序：名称、Model、Endpoint、temperature */
function presetInputs(): HTMLInputElement[] {
  return screen.getAllByRole("textbox") as unknown as HTMLInputElement[];
}

function fill(values: { name?: string; model?: string; baseUrl?: string; key?: string }) {
  const [name, model, baseUrl] = presetInputs();
  if (values.name !== undefined) fireEvent.change(name, { target: { value: values.name } });
  if (values.model !== undefined) fireEvent.change(model, { target: { value: values.model } });
  if (values.baseUrl !== undefined) fireEvent.change(baseUrl, { target: { value: values.baseUrl } });
  if (values.key !== undefined) {
    const keyInput = document.querySelector('input[type="password"]') as HTMLInputElement;
    fireEvent.change(keyInput, { target: { value: values.key } });
  }
}

function renderPage() {
  return render(<ConfigPage onNext={vi.fn()} />);
}

describe("ConfigPage", () => {
  beforeEach(() => {
    api.get.mockImplementation(async (url: string) =>
      url === "/api/presets" ? presets : url === "/api/ragflow/connections" ? conns : []);
    api.post.mockResolvedValue({});
    api.put.mockResolvedValue({});
    api.patch.mockResolvedValue({});
    api.del.mockResolvedValue({});
  });

  it("加载后渲染预设列表与连接列表", async () => {
    renderPage();
    expect(await screen.findByText("默认模型")).toBeInTheDocument();
    expect(screen.getByText("默认 · 当前生效")).toBeInTheDocument();
    expect(screen.getByText("已保存预设（2）")).toBeInTheDocument();
    expect(screen.getByText("本机 RAGFlow")).toBeInTheDocument();
    expect(screen.getByText("已保存连接（1）")).toBeInTheDocument();
  });

  it("名称或 Model 为空时拦截保存，不调接口", () => {
    renderPage();
    fireEvent.click(screen.getByText("新增配置"));
    expect(pushErrorMock).toHaveBeenCalledWith("模型配置·保存", "名称与模型名为必填");
    expect(api.post).not.toHaveBeenCalled();
  });

  it("API Key 栏填成 URL 时先给表单内提示，提交被拦截", () => {
    renderPage();
    fill({ name: "新预设", model: "qwen-max", key: "https://dashscope.aliyuncs.com" });
    expect(screen.getByText(/看起来你把 URL 填进了 API Key 栏/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("新增配置"));
    expect(api.post).not.toHaveBeenCalled();
    expect(pushErrorMock).toHaveBeenCalledWith(
      "模型配置·保存", "API Key 栏填的是 URL，不是密钥", expect.stringContaining("你填的是："),
    );
  });

  it("API Key 填成 URL 的即时提示带读屏属性，且无需提交", () => {
    renderPage();
    fill({ name: "新预设", model: "qwen-max", key: "https://dashscope.aliyuncs.com" });
    // 边输入边提示，此时还没点提交
    expect(screen.getByText(/看起来你把 URL 填进了 API Key 栏/)).toBeInTheDocument();
    const keyInput = document.querySelector('input[type="password"]') as HTMLInputElement;
    expect(keyInput).toHaveAttribute("aria-invalid", "true");
    expect(keyInput).toHaveAttribute("aria-describedby", "preset-api-key-error");
    expect(document.getElementById("preset-api-key-error")).toHaveTextContent("看起来你把 URL 填进了 API Key 栏");
    // 改成正常密钥后提示与属性一并撤回
    fireEvent.change(keyInput, { target: { value: "sk-xxxx" } });
    expect(screen.queryByText(/看起来你把 URL 填进了 API Key 栏/)).toBeNull();
    expect(keyInput).toHaveAttribute("aria-invalid", "false");
    expect(keyInput).not.toHaveAttribute("aria-describedby");
  });

  it("新增同名同 kind 预设被拦截", async () => {
    renderPage();
    // 同名判定依赖已加载的预设列表，先等列表渲染出来
    await screen.findByText("默认模型");
    fill({ name: "默认模型", model: "qwen-max" });
    fireEvent.click(screen.getByText("新增配置"));
    expect(api.post).not.toHaveBeenCalled();
    expect(pushErrorMock).toHaveBeenCalledWith(
      "模型配置·保存", "已存在同名预设「默认模型」", expect.any(String),
    );
  });

  it("新建预设成功后提交 payload 并显示落盘提示", async () => {
    renderPage();
    fill({ name: "新预设", model: "qwen-max" });
    fireEvent.click(screen.getByText("新增配置"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/api/presets", expect.objectContaining({
      kind: "llm", name: "新预设", model: "qwen-max", provider: "openai_compatible",
      base_url: "",
    }));
    expect(await screen.findByText("预设已保存（密钥已加密落盘）")).toBeInTheDocument();
  });

  it("点编辑回填表单，保存修改走 PUT", async () => {
    renderPage();
    await screen.findByText("默认模型");
    fireEvent.click(screen.getAllByText("编辑")[0]);
    expect(screen.getByText("编辑中：默认模型")).toBeInTheDocument();
    expect(presetInputs()[0].value).toBe("默认模型");

    fill({ model: "qwen-plus-2025" });
    fireEvent.click(screen.getByText("保存修改"));
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(api.put).toHaveBeenCalledWith("/api/presets/p1", expect.objectContaining({
      name: "默认模型", model: "qwen-plus-2025",
    }));
    expect(await screen.findByText("预设已更新")).toBeInTheDocument();
    // 保存后退出编辑态，按钮回到「新增配置」
    expect(screen.getByText("新增配置")).toBeInTheDocument();
  });

  it("取消编辑清空表单并退出编辑态", async () => {
    renderPage();
    await screen.findByText("默认模型");
    fireEvent.click(screen.getAllByText("编辑")[0]);
    fireEvent.click(screen.getByText("取消编辑"));
    expect(screen.getByText("新增 LLM 预设")).toBeInTheDocument();
    expect(presetInputs()[0].value).toBe("");
  });

  it("连通自检分别展示成功与失败文案", async () => {
    renderPage();
    await screen.findByText("默认模型");
    api.post.mockResolvedValueOnce({ ok: true, latency_ms: 42 });
    fireEvent.click(screen.getAllByText("连通自检")[0]);
    expect(await screen.findByText("连通正常 · 42ms")).toBeInTheDocument();

    api.post.mockResolvedValueOnce({ ok: false, latency_ms: 0, detail: "Request timed out" });
    fireEvent.click(screen.getAllByText("连通自检")[0]);
    expect(await screen.findByText("连通失败：Request timed out")).toBeInTheDocument();
  });

  it("设为默认调用 set-default 接口并带上 kind", async () => {
    renderPage();
    await screen.findByText("备用模型");
    fireEvent.click(screen.getByText("设为默认"));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(
      "/api/presets/p2/set-default?kind=llm", {},
    ));
  });

  it("新增连接缺服务地址或 Key 时给出表单内错误", () => {
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("例如：本机 RAGFlow"), { target: { value: "新连接" } });
    fireEvent.click(screen.getByText("新增连接"));
    expect(screen.getByText("新增连接必须填写名称、服务地址和 API Key")).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("编辑连接改地址但未重填 Key 时拦截", async () => {
    renderPage();
    await screen.findByText("本机 RAGFlow");
    // 连接行的「编辑」（预设行也在，取最后一个）
    const editButtons = screen.getAllByText("编辑");
    fireEvent.click(editButtons[editButtons.length - 1]);
    fireEvent.change(screen.getByPlaceholderText("http://127.0.0.1:9380"), {
      target: { value: "http://127.0.0.1:9381" },
    });
    fireEvent.click(screen.getByText("保存修改"));
    expect(screen.getByText(/服务地址已变更，请重新填写 API Key/)).toBeInTheDocument();
    expect(api.patch).not.toHaveBeenCalled();
  });

  it("必填拦截的文案显示在对应输入框下方", () => {
    renderPage();
    fireEvent.click(screen.getByText("新增配置"));
    expect(screen.getByText(/预设名称为必填/)).toBeInTheDocument();
    expect(screen.getByText(/Model 为必填/)).toBeInTheDocument();
    // 读屏也要能听到原因：标记无效字段并指向错误文案
    expect(presetInputs()[0]).toHaveAttribute("aria-invalid", "true");
    expect(presetInputs()[0]).toHaveAttribute("aria-describedby", "preset-name-error");
    expect(presetInputs()[1]).toHaveAttribute("aria-describedby", "preset-model-error");
    expect(document.getElementById("preset-name-error")).toHaveTextContent("预设名称为必填");
    expect(document.getElementById("preset-model-error")).toHaveTextContent("Model 为必填");
    // 仍保留调试抽屉里的原始记录，便于排查
    expect(pushErrorMock).toHaveBeenCalledWith("模型配置·保存", "名称与模型名为必填");
    expect(api.post).not.toHaveBeenCalled();
  });

  it("同名拦截的文案显示在名称输入框下方", async () => {
    renderPage();
    await screen.findByText("默认模型");
    fill({ name: "默认模型", model: "qwen-max" });
    fireEvent.click(screen.getByText("新增配置"));
    expect(screen.getByText(/已存在同名预设「默认模型」/)).toBeInTheDocument();
    expect(presetInputs()[0]).toHaveAttribute("aria-invalid", "true");
    expect(presetInputs()[0]).toHaveAttribute("aria-describedby", "preset-duplicate-error");
    expect(document.getElementById("preset-duplicate-error")).toHaveTextContent("已存在同名预设「默认模型」");
    expect(api.post).not.toHaveBeenCalled();
  });

  it("设为默认与删除预设后刷新失败也不产生未处理拒绝", async () => {
    renderPage();
    await screen.findByText("备用模型");
    // 两个写操作成功后的刷新都失败
    api.get.mockImplementation(async (url: string) => {
      if (url === "/api/presets") throw new Error("后端离线");
      return url === "/api/ragflow/connections" ? conns : [];
    });
    fireEvent.click(screen.getByText("设为默认"));
    await waitFor(() => expect(pushErrorMock).toHaveBeenCalledWith(
      "模型配置·默认", "设为默认成功，但刷新预设列表失败", expect.stringContaining("后端离线"),
    ));
    fireEvent.click(screen.getAllByText("删除")[0]);
    await waitFor(() => expect(pushErrorMock).toHaveBeenCalledWith(
      "模型配置·删除", "已删除 默认模型，但刷新预设列表失败", expect.stringContaining("后端离线"),
    ));
    // 让微任务队列清空，未处理的 rejection 才可能被检测到
    await new Promise((r) => setTimeout(r, 0));
  });

  it("保存成功后刷新列表失败不产生未处理拒绝", async () => {
    renderPage();
    await screen.findByText("默认模型");
    fill({ name: "新预设", model: "qwen-max" });
    // 让保存成功之后的这次 load() 失败
    api.get.mockImplementation(async (url: string) => {
      if (url === "/api/presets") throw new Error("后端离线");
      return url === "/api/ragflow/connections" ? conns : [];
    });
    fireEvent.click(screen.getByText("新增配置"));
    // 刷新失败由 save 的 catch 兜住并通过 pushError 报出；
    // 若漏掉 await/catch，这个 rejected promise 会由 vitest 记为未处理拒绝并让本次运行失败
    await waitFor(() => expect(pushErrorMock).toHaveBeenCalledWith(
      "模型配置·保存", "保存成功，但刷新预设列表失败", expect.stringContaining("后端离线"),
    ));
    // 让微任务队列清空，未处理的 rejection 才可能被检测到
    await new Promise((r) => setTimeout(r, 0));
    expect(await screen.findByText("预设已保存（密钥已加密落盘）")).toBeInTheDocument();
  });

  it("连接测试 verdict=graph_empty 提示无可用素材而非成功", async () => {
    renderPage();
    await screen.findByText("本机 RAGFlow");
    api.post.mockResolvedValueOnce({ verdict: "graph_empty", stages: [] });
    fireEvent.click(screen.getByText("测试"));
    expect(await screen.findByText(/连通正常，但没有可用素材/)).toBeInTheDocument();
    expect(pushErrorMock).toHaveBeenCalledWith(
      "RAGFlow 连接·测试", expect.stringContaining("本机 RAGFlow"),
    );
  });

  it("连接测试结果显示探测到的 RAGFlow 版本", async () => {
    renderPage();
    await screen.findByText("本机 RAGFlow");
    api.post.mockResolvedValueOnce({ verdict: "ready", stages: [], probed_version: "1.0.0" });
    fireEvent.click(screen.getByText("测试"));
    expect(await screen.findByTestId("conn-test-version")).toHaveTextContent("RAGFlow 1.0.0");
  });

  it("连接测试拿不到版本时明确写「版本未知」，不留空白", async () => {
    renderPage();
    await screen.findByText("本机 RAGFlow");
    api.post.mockResolvedValueOnce({ verdict: "ready", stages: [], probed_version: null });
    fireEvent.click(screen.getByText("测试"));
    expect(await screen.findByTestId("conn-test-version")).toHaveTextContent("版本未知");
  });

  it("连不上 / 鉴权失败时不追加版本行，避免「版本未知」变成噪音", async () => {
    renderPage();
    await screen.findByText("本机 RAGFlow");
    api.post.mockResolvedValueOnce({ verdict: "unreachable", stages: [], probed_version: null });
    fireEvent.click(screen.getByText("测试"));
    await screen.findByText(/连不上 RAGFlow/);
    expect(screen.queryByTestId("conn-test-version")).not.toBeInTheDocument();
  });

  it("测过的连接在列表行常驻显示版本；没有版本时写「版本未知」", async () => {
    api.get.mockImplementation(async (url: string) => {
      if (url === "/api/presets") return presets;
      if (url === "/api/ragflow/connections") {
        return [
          { ...conns[0], probed_version: "1.0.0", last_test: { verdict: "ready", at: "2026-11-14 10:00" } },
          { ...conns[0], id: "c2", name: "旧连接", probed_version: null, last_test: { verdict: "ready", at: "2026-11-14 10:05" } },
        ];
      }
      return [];
    });
    renderPage();
    expect(await screen.findByText("RAGFlow 1.0.0")).toBeInTheDocument();
    expect(screen.getByText("版本未知")).toBeInTheDocument();
  });
});
