import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DocItem } from "../types";

// 保留 fmtBytes 等纯函数，只替换网络相关导出
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, get: vi.fn(), post: vi.fn(), del: vi.fn(), uploadFiles: vi.fn() };
});
vi.mock("../lib/errorLog", () => ({ pushError: vi.fn() }));

import DocumentsPage from "./DocumentsPage";
import { del, get, post, uploadFiles } from "../api";

const api = {
  get: vi.mocked(get as unknown as (url: string) => Promise<unknown>),
  post: vi.mocked(post as unknown as (url: string, body?: unknown) => Promise<unknown>),
  del: vi.mocked(del as unknown as (url: string) => Promise<unknown>),
  upload: vi.mocked(uploadFiles as unknown as (files: FileList | File[]) => Promise<unknown>),
};

const docs: DocItem[] = [
  {
    id: "doc-1", name: "说明.md", size: 2048, est_tokens: 617, chars: 1200,
    source: "upload", status: "ready",
  },
  {
    id: "doc-2", name: "接口.txt", size: 1024, est_tokens: 300, chars: 600,
    source: "upload", status: "ready",
  },
];

function uploadInput(): HTMLInputElement {
  return document.querySelector('input[type="file"]:not([webkitdirectory])') as HTMLInputElement;
}

function renderPage() {
  const onNext = vi.fn();
  render(<DocumentsPage onNext={onNext} />);
  return { onNext };
}

describe("DocumentsPage", () => {
  beforeEach(() => {
    api.get.mockResolvedValue(docs);
    api.post.mockResolvedValue({ total_tokens: 1234, avg_tokens: 617, bin: "long", warning: null });
    api.del.mockResolvedValue({});
    api.upload.mockResolvedValue({});
  });

  it("加载文档列表并渲染 token 预估", async () => {
    renderPage();
    expect(await screen.findByText("已加载文档（2）")).toBeInTheDocument();
    expect(screen.getByText("说明.md")).toBeInTheDocument();
    expect(screen.getByText("1,234")).toBeInTheDocument();
    expect(screen.getByText("平均 617 tokens/文档")).toBeInTheDocument();
    expect(await screen.findByText(">500 tokens · 长文档管线")).toBeInTheDocument();
  });

  it("列表加载失败时把原因写到页面提示", async () => {
    api.get.mockRejectedValue(new Error("后端离线"));
    renderPage();
    expect(await screen.findByText("文档列表加载失败：后端离线")).toBeInTheDocument();
    expect(screen.getByText("暂无文档。")).toBeInTheDocument();
  });

  it("选择文件后上传并提示数量", async () => {
    renderPage();
    await screen.findByText("已加载文档（2）");
    const file = new File(["# 标题"], "新文档.md", { type: "text/markdown" });
    fireEvent.change(uploadInput(), { target: { files: [file] } });
    await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("已上传 1 个文件")).toBeInTheDocument();
  });

  it("拖拽文件到上传卡片同样触发上传", async () => {
    renderPage();
    await screen.findByText("已加载文档（2）");
    const file = new File(["内容"], "拖拽文档.md", { type: "text/markdown" });
    fireEvent.drop(screen.getByRole("button", { name: "点击或拖拽上传文档" }), {
      dataTransfer: { files: [file] },
    });
    await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("已上传 1 个文件")).toBeInTheDocument();
  });

  it("文件夹选择只保留支持格式并提示", async () => {
    renderPage();
    await screen.findByText("已加载文档（2）");
    const folderInput = document.querySelector('input[webkitdirectory]') as HTMLInputElement;
    const md = new File(["a"], "a.md", { type: "text/markdown" });
    const png = new File(["b"], "b.png", { type: "image/png" });
    fireEvent.change(folderInput, { target: { files: [md, png] } });
    expect(await screen.findByText("已选中 1 个支持格式的文件")).toBeInTheDocument();
    expect(screen.getByText("匹配文件（1）")).toBeInTheDocument();
    expect(screen.getByText("确认读取（1 个文件）")).toBeInTheDocument();
  });

  it("文件夹里没有支持格式时给出格式清单", async () => {
    renderPage();
    await screen.findByText("已加载文档（2）");
    const folderInput = document.querySelector('input[webkitdirectory]') as HTMLInputElement;
    fireEvent.change(folderInput, {
      target: { files: [new File(["b"], "b.png", { type: "image/png" })] },
    });
    expect(await screen.findByText("该文件夹中没有支持的文件格式（.md / .html / .docx / .txt）")).toBeInTheDocument();
  });

  it("确认读取文件夹把匹配文件交给上传接口", async () => {
    renderPage();
    await screen.findByText("已加载文档（2）");
    const folderInput = document.querySelector('input[webkitdirectory]') as HTMLInputElement;
    fireEvent.change(folderInput, {
      target: { files: [new File(["a"], "a.md", { type: "text/markdown" })] },
    });
    fireEvent.click(await screen.findByText("确认读取（1 个文件）"));
    await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("已从文件夹读取 1 个文件")).toBeInTheDocument();
  });

  it("移除文档先确认：取消不调接口，确认后删除并刷新", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPage();
    await screen.findByText("说明.md");
    fireEvent.click(screen.getAllByText("移除")[0]);
    expect(api.del).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    fireEvent.click(screen.getAllByText("移除")[0]);
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/api/documents/doc-1"));
    // 删除后会重新拉列表
    await waitFor(() => expect(api.get.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("有文档时显示下一步并触发回调", async () => {
    const { onNext } = renderPage();
    await screen.findByText("说明.md");
    fireEvent.click(screen.getByText("生成知识图谱 →"));
    expect(onNext).toHaveBeenCalledTimes(1);
  });
});
